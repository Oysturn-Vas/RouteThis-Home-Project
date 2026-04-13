import os
import argparse
from typing import List
import time
from dotenv import load_dotenv
import fitz  # PyMuPDF
import io
from PIL import Image
from google import genai
from google.genai.errors import APIError
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from pinecone import Pinecone, ServerlessSpec

# Load environment variables
load_dotenv()

# Configuration
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
PINECONE_API_KEY = os.getenv("PINECONE_API_KEY")
PINECONE_INDEX_NAME = os.getenv("PINECONE_INDEX_NAME", "routemaster-manuals")
EMBEDDING_MODEL = "models/gemini-embedding-001"
EMBEDDING_DIMENSION = 3072  # Google embeddings dimension

# Configure new Google GenAI Client for vision tasks
client = genai.Client(api_key=GOOGLE_API_KEY)
vision_model_id = 'gemini-2.5-flash'

def init_pinecone() -> Pinecone:
    """Initialize Pinecone client and ensure the index exists with correct dimensions."""
    if not PINECONE_API_KEY:
        raise ValueError("PINECONE_API_KEY is not set in environment variables.")
    
    pc = Pinecone(api_key=PINECONE_API_KEY)
    
    # Check if index exists and has correct dimensions
    existing_indexes = [index_info["name"] for index_info in pc.list_indexes()]
    if PINECONE_INDEX_NAME in existing_indexes:
        # If it exists, check dimensions. If wrong, delete it.
        info = pc.describe_index(PINECONE_INDEX_NAME)
        if info.dimension != EMBEDDING_DIMENSION:
            print(f"Deleting existing index '{PINECONE_INDEX_NAME}' due to dimension mismatch ({info.dimension} vs {EMBEDDING_DIMENSION})...")
            pc.delete_index(PINECONE_INDEX_NAME)
            existing_indexes.remove(PINECONE_INDEX_NAME)
            time.sleep(2) # Give Pinecone a moment
            
    if PINECONE_INDEX_NAME not in existing_indexes:
        print(f"Creating Pinecone index: '{PINECONE_INDEX_NAME}' with dimension {EMBEDDING_DIMENSION}...")
        pc.create_index(
            name=PINECONE_INDEX_NAME,
            dimension=EMBEDDING_DIMENSION,
            metric="cosine",
            spec=ServerlessSpec(
                cloud="aws",
                region="us-east-1"
            )
        )
    return pc

def process_pdf(file_path: str, model_name: str) -> List[dict]:
    """Parse the PDF, extract text and images, use Gemini to caption images, and split into chunks."""
    print(f"Reading PDF from {file_path}...")
    doc = fitz.open(file_path)
    
    # Initialize the text splitter
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=2500,
        chunk_overlap=400,
        length_function=len,
        separators=["\n\n\n", "\n\n", "\n", ". ", ", ", " ", ""]
    )
    
    chunks = []
    
    for page_num in range(len(doc)):
        print(f"Processing Page {page_num + 1}/{len(doc)}...")
        page = doc[page_num]
        page_text = page.get_text()
        
        # Extract and caption images
        image_list = page.get_images(full=True)
        image_captions = []
        
        for img_index, img in enumerate(image_list):
            try:
                xref = img[0]
                base_image = doc.extract_image(xref)
                image_bytes = base_image["image"]
                image_ext = base_image["ext"]
                
                # Filter out tiny insignificant images (like UI icons) to save API calls
                if len(image_bytes) < 5000: 
                    continue

                # Convert to PIL Image
                pil_img = Image.open(io.BytesIO(image_bytes))
                
                # Generate Caption with Gemini
                prompt = (
                    "You are a technical document analyzer. Describe this image in high detail. "
                    "If it is a physical diagram of a router, describe the ports, buttons, LED lights, and labels visible. "
                    "If it is a UI screenshot, describe the settings visible. Keep it concise but highly technical."
                )
                
                # Add retry loop for rate limiting
                max_retries = 3
                caption = ""
                for attempt in range(max_retries):
                    try:
                        response = client.models.generate_content(
                            model=vision_model_id,
                            contents=[pil_img, prompt]
                        )
                        caption = response.text.strip()
                        print(f"  - Caption generated for image {img_index + 1}")
                        break
                    except Exception as gen_err:
                        if '429' in str(gen_err) or 'Quota exceeded' in str(gen_err):
                            print(f"  - Rate limit hit. Waiting 60 seconds (Attempt {attempt+1}/{max_retries})...")
                            time.sleep(60)
                        else:
                            raise gen_err
                
                if caption:
                    image_captions.append(f"\n[Image Diagram/Screenshot on Page {page_num + 1}]: {caption}\n")
                
            except Exception as e:
                print(f"  - Warning: Failed to process image {img_index + 1} on page {page_num + 1}: {e}")
                
        # Fuse Text and Image Captions
        combined_page_content = page_text + "\n" + "\n".join(image_captions)
        
        if not combined_page_content.strip():
            continue
            
        # Split text into chunks
        page_chunks = text_splitter.split_text(combined_page_content)
        
        for chunk in page_chunks:
            chunks.append({
                "text": chunk,
                "metadata": {
                    "model": model_name,
                    "page": page_num + 1,
                    "source": os.path.basename(file_path)
                }
            })
            
    print(f"Total chunks created: {len(chunks)}")
    return chunks

def embed_and_upsert(pc: Pinecone, chunks: List[dict]):
    """Generate embeddings for chunks and upsert to Pinecone."""
    print(f"Initializing Google embeddings with model {EMBEDDING_MODEL}...")
    embeddings = GoogleGenerativeAIEmbeddings(model=EMBEDDING_MODEL, google_api_key=GOOGLE_API_KEY)
    index = pc.Index(PINECONE_INDEX_NAME)
    
    batch_size = 100
    for i in range(0, len(chunks), batch_size):
        batch = chunks[i:i + batch_size]
        
        # Extract text for embedding
        texts = [item["text"] for item in batch]
        
        # Generate embeddings
        print(f"Embedding batch {i} to {i + len(batch)}...")
        embedded_vectors = embeddings.embed_documents(texts)
        
        # Prepare vectors for upsert
        vectors_to_upsert = []
        for j, (vector, item) in enumerate(zip(embedded_vectors, batch)):
            vector_id = f"{item['metadata']['model']}-chunk-{i + j}"
            
            # Combine text into metadata for retrieval
            metadata = item["metadata"].copy()
            metadata["text"] = item["text"]
            
            vectors_to_upsert.append({
                "id": vector_id,
                "values": vector,
                "metadata": metadata
            })
            
        # Upsert batch
        print(f"Upserting batch {i} to {i + len(batch)} into Pinecone...")
        index.upsert(vectors=vectors_to_upsert)

    print("Ingestion complete!")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest EA6350 manual with Multimodal Vision into Pinecone.")
    parser.add_argument("--pdf", type=str, required=True, help="Path to the PDF manual.")
    parser.add_argument("--model", type=str, default="EA6350", help="Router model name for metadata.")
    
    args = parser.parse_args()
    
    if not os.path.exists(args.pdf):
        print(f"Error: File {args.pdf} does not exist.")
        exit(1)
        
    try:
        pc_client = init_pinecone()
        document_chunks = process_pdf(args.pdf, args.model)
        embed_and_upsert(pc_client, document_chunks)
    except Exception as e:
        print(f"An error occurred during ingestion: {e}")