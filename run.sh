# Basic conversion (English default)
#ocrmypdf ocrsample.pdf output_searchable.pdf

# Specifying language (e.g., Kannada + English)
ocrmypdf -l eng input.pdf output_searchable_eng.pdf

# Redo OCR if bad text layer already exists
ocrmypdf --redo-ocr ocrsample.pdf output_searchable_redo.pdf