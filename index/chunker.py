from langchain_text_splitters import RecursiveCharacterTextSplitter

def chunk(docs, size: int = 800, overlap: int = 120):
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=size, chunk_overlap=overlap,
        separators=["\n\n", "\n", ". ", " ", ""]
    )
    return splitter.split_documents(docs)
