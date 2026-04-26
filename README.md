# FDD Item Splitter

A user-friendly Streamlit app that splits FDD PDFs into separate PDFs for:

- Front End
- Items 1–21
- Franchise Agreement / Contract

## For end users

1. Open the app link.
2. Drag one or more FDD PDF files into the upload box.
3. Click **Split FDDs**.
4. Download the ZIP file when processing is complete.

No coding or command line is needed.

## Deploy on Streamlit Community Cloud

1. Create a GitHub repository, for example `fdd-item-splitter`.
2. Upload these files to the repository:
   - `app.py`
   - `fdd_parser_core_locked.py`
   - `requirements.txt`
   - `.streamlit/config.toml`
3. Go to Streamlit Community Cloud.
4. Click **New app**.
5. Select the GitHub repository.
6. Set the main file path to `app.py`.
7. Click **Deploy**.

## Local testing

```bash
pip install -r requirements.txt
streamlit run app.py
```

## Important

The parser logic is intentionally isolated in `fdd_parser_core_locked.py`. The Streamlit app should only handle upload, progress display, status reporting, and ZIP download.
