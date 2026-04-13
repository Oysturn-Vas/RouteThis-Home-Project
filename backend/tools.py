import logging
from typing import Optional
from pinecone import Pinecone
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from google import genai
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import settings

logger = logging.getLogger("routemaster-tools")

pc = Pinecone(api_key=settings.PINECONE_API_KEY)
index = pc.Index(settings.PINECONE_INDEX_NAME)
embeddings = GoogleGenerativeAIEmbeddings(model="models/gemini-embedding-001", google_api_key=settings.GOOGLE_API_KEY)

client = genai.Client(api_key=settings.GOOGLE_API_KEY)
eval_model_id = 'gemini-2.5-flash'


class RouteMasterTools:
    def __init__(self, tools: Optional[list] = None):
        self.tools = tools or []

    async def query_manual(self, query: str) -> str:
        """Searches the Pinecone database, drafts an answer, and evaluates it for hallucinations."""
        logger.info(f"RAG Tool called with query: {query}")
        try:
            query_vector = embeddings.embed_query(query)

            search_response = index.query(
                vector=query_vector,
                top_k=3,
                include_metadata=True,
                filter={"model": {"$eq": "EA6350"}}
            )

            if not search_response.matches:
                return "I could not find a verified answer in the manual for that specific request."

            context_chunks = []
            for match in search_response.matches:
                page = match.metadata.get("page", "Unknown")
                text = match.metadata.get("text", "")
                context_chunks.append(f"[Page {page}]: {text}")

            source_context = "\n\n".join(context_chunks)
            logger.info(f"Retrieved {len(search_response.matches)} chunks from manual.")

            draft_prompt = f"""Based ONLY on the following manual excerpts, answer the user's query: "{query}".
Provide clear, step-by-step instructions if applicable. Do not add any outside knowledge.

Source Text:
{source_context}"""

            draft_response = await client.aio.models.generate_content(
                model=eval_model_id,
                contents=draft_prompt
            )
            draft_answer = draft_response.text.strip()

            eval_prompt = f"""You are a strict technical safety evaluator. 
Compare the 'Draft Answer' against the 'Original Source Text'.
Your task: Does the Draft Answer contain any steps, numbers, or technical instructions that are NOT explicitly present in the Original Source Text?
Output exactly 'PASS' if it is perfectly accurate and grounded in the source text. 
Output exactly 'FAIL' if there is any hallucination, added information, or missing critical warnings.

Original Source Text:
{source_context}

Draft Answer:
{draft_answer}"""

            eval_response = await client.aio.models.generate_content(
                model=eval_model_id,
                contents=eval_prompt
            )
            judgment = eval_response.text.strip().upper()

            if "FAIL" in judgment:
                logger.warning(
                    f"RAG Guardrail tripped! Fallback returned. "
                    f"Query: '{query}'. Failed Answer: '{draft_answer}'"
                )
                return "I could not find a verified answer in the manual for that specific request."

            logger.info("RAG Guardrail passed.")
            return draft_answer

        except Exception as e:
            logger.error(f"Error querying manual: {e}")
            return "An error occurred while accessing the manual database."