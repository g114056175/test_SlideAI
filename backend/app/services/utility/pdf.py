import pypdf as PyPDF2

def pdf_to_text_array(pdf_path):
    texts = []
    # Open the PDF file in binary mode
    with open(pdf_path, 'rb') as file:
        # Create a PDF reader object
        reader = PyPDF2.PdfReader(file)
        # Iterate through all the pages and extract text
        for page in reader.pages:
            page_text = page.extract_text() or ""
            texts.append(str(page_text).strip())
    return texts
