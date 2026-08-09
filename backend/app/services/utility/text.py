import re

def remove_markdown(text):
    """
    Removes common Markdown syntax from the given text.
    """
    text = re.sub(r'`([^`]*)`', r'\1', text)  # Inline code `code`
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)  # Bold **text**
    text = re.sub(r'__(.*?)__', r'\1', text)  # Bold __text__
    text = re.sub(r'\*(.*?)\*', r'\1', text)  # Italic *text*
    text = re.sub(r'_(.*?)_', r'\1', text)  # Italic _text_
    text = re.sub(r'#{1,6}\s*(.*)', r'\1', text)  # Headers # H1, ## H2, etc.
    text = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', text)  # Links [text](url)
    text = re.sub(r'>\s*(.*)', r'\1', text)  # Blockquotes > text
    text = re.sub(r'^\s*[-+*]\s+', '', text, flags=re.MULTILINE)  # Lists - item, * item
    text = re.sub(r'\|', '', text)  # Remove table pipes "|"
    
    return text.strip()
