import logging
import re
from pinecone import Pinecone
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from google import genai

from config import settings

logger = logging.getLogger("routemaster-rag")

pc = Pinecone(api_key=settings.PINECONE_API_KEY)
index = pc.Index(settings.PINECONE_INDEX_NAME)

embeddings = GoogleGenerativeAIEmbeddings(
    model="models/gemini-embedding-001", google_api_key=settings.GOOGLE_API_KEY
)

client = genai.Client(api_key=settings.GOOGLE_API_KEY)
EVAL_MODEL_ID = settings.GEMINI_MODEL_ID


def clean_rag_answer(answer: str) -> str:
    answer = re.sub(
        r"<system-reminder>.*?</system-reminder>", "", answer, flags=re.DOTALL
    )
    answer = answer.replace("Based ONLY on the provided manual excerpts, ", "")
    answer = answer.replace("Based on the provided manual excerpts, ", "")
    return answer.strip()


async def query_knowledge_base(query: str) -> dict:
    """Searches the Pinecone database, drafts an answer, and evaluates it for hallucinations.

    Returns:
        dict with keys:
            - success: bool
            - context: str - The formatted context from manual (empty if no matches)
            - answer: str - The drafted and verified answer
            - verified: bool - True if RAG answer passed evaluation, False otherwise
            - error: str or None
            - retryable: bool
    """
    logger.info(f"[RAG-PIPELINE] ===========================================")
    logger.info(f"[RAG-PIPELINE] query_knowledge_base() CALLED")
    logger.info(f"[RAG-PIPELINE] Query: '{query}'")
    logger.info(f"[RAG-PIPELINE] ===========================================")

    try:
        logger.info(f"[RAG-PIPELINE] Step 1: Generating embedding for query...")
        query_vector = embeddings.embed_query(query)
        logger.info(
            f"[RAG-PIPELINE] Embedding generated, dimension: {len(query_vector)}"
        )

        logger.info(
            f"[RAG-PIPELINE] Step 2: Querying Pinecone index '{settings.PINECONE_INDEX_NAME}'..."
        )
        search_response = index.query(
            vector=query_vector,
            top_k=3,
            include_metadata=True,
            filter={"model": {"$eq": "EA6350"}},
        )
        logger.info(
            f"[RAG-PIPELINE] Pinecone returned {len(search_response.matches) if search_response.matches else 0} matches"
        )

        if not search_response.matches:
            logger.warning(
                f"[RAG-PIPELINE] No matches found in Pinecone for query: '{query}'"
            )
            return {
                "success": True,
                "context": "",
                "answer": "No relevant information found in the manual for this query.",
                "verified": False,
                "error": None,
                "retryable": False,
            }

        context_chunks = []
        for idx, match in enumerate(search_response.matches):
            page = match.metadata.get("page", "Unknown")
            text = match.metadata.get("text", "")
            score = match.score if hasattr(match, "score") else "N/A"
            context_chunks.append(f"[Page {page}]: {text}")
            logger.info(
                f"[RAG-PIPELINE]   Match {idx + 1}: page={page}, score={score}, text_len={len(text)}"
            )

        source_context = "\n\n".join(context_chunks)
        logger.info(
            f"[RAG-PIPELINE] Built source context from {len(context_chunks)} chunks"
        )

        logger.info(f"[RAG-PIPELINE] Step 3: Drafting answer using {EVAL_MODEL_ID}...")
        draft_prompt = f"""Based ONLY on the following manual excerpts, answer the user's query: "{query}".
Provide clear, step-by-step instructions if applicable. Do not add any outside knowledge.

Source Text:
{source_context}"""

        draft_response = await client.aio.models.generate_content(
            model=EVAL_MODEL_ID, contents=draft_prompt
        )
        draft_answer = clean_rag_answer(draft_response.text.strip())
        logger.info(
            f"[RAG-PIPELINE] Draft answer generated ({len(draft_answer)} chars):\n{draft_answer}"
        )

        logger.info(f"[RAG-PIPELINE] Step 4: Evaluating draft for hallucinations...")
        eval_prompt = f"""You are a helpful technical support assistant.
Compare the 'Draft Answer' against the 'Original Source Text'.
Your task: Is the Draft Answer a helpful and accurate summary of the Original Source Text? The Draft Answer does not need to be a word-for-word match, but it must be consistent with the source text and not contain any contradictions or unsafe instructions.
Output exactly 'PASS' if the answer is helpful and consistent.
Output exactly 'FAIL' if the answer is misleading, contradictory, or unsafe.

Original Source Text:
{source_context}

Draft Answer:
{draft_answer}"""

        eval_response = await client.aio.models.generate_content(
            model=EVAL_MODEL_ID, contents=eval_prompt
        )
        judgment = eval_response.text.strip().upper()
        logger.info(f"[RAG-PIPELINE] Evaluation judgment: '{judgment}'")

        if "FAIL" in judgment:
            logger.warning(
                f"[RAG-PIPELINE] RAG GUARDRAIL TRIPPED - Answer could not be verified"
            )
            logger.warning(f"[RAG-PIPELINE] Query: '{query}'")
            logger.warning(f"[RAG-PIPELINE] Draft Answer:\n{draft_answer}")
            return {
                "success": True,
                "context": source_context,
                "answer": "I found some information in the manual but couldn't fully verify its accuracy. Please consult the official documentation for complete and verified instructions.",
                "verified": False,
                "error": None,
                "retryable": False,
            }

        logger.info(f"[RAG-PIPELINE] RAG SUCCESS - Answer verified and approved")
        logger.info(
            f"[RAG-PIPELINE] Final answer ({len(draft_answer)} chars):\n{draft_answer}"
        )
        logger.info(f"[RAG-PIPELINE] ===========================================")
        return {
            "success": True,
            "context": source_context,
            "answer": draft_answer,
            "verified": True,
            "error": None,
            "retryable": False,
        }

    except Exception as e:
        logger.error(f"[RAG-PIPELINE] ERROR: {e}")
        return {
            "success": False,
            "context": "",
            "answer": None,
            "verified": False,
            "error": str(e),
            "retryable": True,
        }
