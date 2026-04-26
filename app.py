from __future__ import annotations

import io
import tempfile
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Dict, List

import streamlit as st

from fdd_parser_core_locked import parse_debug_items, process_pdf


APP_TITLE = "FDD Item Splitter"
DEFAULT_DEBUG_ITEMS = "9,10,11,12,13"


st.set_page_config(
    page_title=APP_TITLE,
    page_icon="📄",
    layout="centered",
)


st.markdown(
    """
    <style>
    .block-container { max-width: 980px; padding-top: 2rem; }
    div[data-testid="stMetricValue"] { font-size: 1.4rem; }
    .small-note { color: #6B7280; font-size: 0.95rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(show_spinner=False)
def _empty_cache_marker() -> str:
    return "ready"


def zip_folder(folder: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(folder.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(folder))
    buffer.seek(0)
    return buffer.getvalue()


def list_generated_pdfs(output_root: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for pdf in sorted(output_root.rglob("*.pdf")):
        rows.append(
            {
                "Source folder": pdf.parent.name,
                "Output PDF": pdf.name,
                "Size KB": round(pdf.stat().st_size / 1024, 1),
            }
        )
    return rows


def user_status_label(status: object) -> str:
    text = str(status or "").upper()
    if text == "OK":
        return "Complete"
    if text == "ERROR":
        return "Needs review"
    if text == "CHECK":
        return "Check output"
    return text.title() or "Unknown"


def simplify_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    simple: List[Dict[str, Any]] = []
    for row in results:
        simple.append(
            {
                "File": row.get("source_pdf", ""),
                "Status": user_status_label(row.get("status", "")),
                "Items found": row.get("items_found", ""),
                "Note": row.get("error", "") or row.get("warning", "") or "",
            }
        )
    return simple


_empty_cache_marker()

st.title(APP_TITLE)
st.write(
    "Upload one or more FDD PDFs. The tool will split each file into the Front End, Items 1–21, and the Franchise Agreement/Contract."
)

with st.expander("How this works", expanded=False):
    st.markdown(
        """
        1. Drag FDD PDF files into the upload box.  
        2. Click **Split FDDs**.  
        3. Review the status table.  
        4. Download the ZIP package of split PDFs.

        The tool does not change your original PDFs. It creates new split PDFs in a downloadable ZIP file.
        """
    )

uploaded_files = st.file_uploader(
    "Upload FDD PDFs",
    type=["pdf"],
    accept_multiple_files=True,
    help="You can upload one PDF or several PDFs at once.",
)

with st.expander("Advanced diagnostics", expanded=False):
    debug = st.checkbox("Show item boundary diagnostics", value=False)
    debug_items_text = st.text_input("Diagnostic items", value=DEFAULT_DEBUG_ITEMS)
    st.caption("Leave this off for normal use. Turn it on only when checking a bad split.")

split_clicked = st.button(
    "Split FDDs",
    type="primary",
    use_container_width=True,
    disabled=not uploaded_files,
)

if not uploaded_files:
    st.info("Upload at least one PDF to begin.")
    st.stop()

if not split_clicked:
    st.stop()

with tempfile.TemporaryDirectory() as tmp:
    work_dir = Path(tmp)
    input_dir = work_dir / "input"
    output_dir = work_dir / "output"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    for uploaded in uploaded_files:
        safe_name = Path(uploaded.name).name
        (input_dir / safe_name).write_bytes(uploaded.getbuffer())

    pdf_paths = sorted(input_dir.glob("*.pdf"))
    results: List[Dict[str, Any]] = []
    combined_log = io.StringIO()
    debug_items = parse_debug_items(debug_items_text)

    progress = st.progress(0, text="Preparing files...")

    for idx, pdf_path in enumerate(pdf_paths, start=1):
        progress.progress(
            (idx - 1) / max(len(pdf_paths), 1),
            text=f"Processing {idx} of {len(pdf_paths)}: {pdf_path.name}",
        )
        try:
            with redirect_stdout(combined_log), redirect_stderr(combined_log):
                result = process_pdf(
                    pdf_path,
                    output_dir,
                    make_text=False,
                    debug=debug,
                    debug_items=debug_items,
                )
            results.append(dict(result))
        except Exception as exc:
            results.append(
                {
                    "source_pdf": pdf_path.name,
                    "status": "ERROR",
                    "items_found": 0,
                    "output_dir": "",
                    "error": str(exc),
                }
            )

    progress.progress(1.0, text="Processing complete.")

    successful = sum(1 for r in results if str(r.get("status", "")).upper() == "OK")
    total = len(results)
    generated = list_generated_pdfs(output_dir)

    col1, col2, col3 = st.columns(3)
    col1.metric("Files processed", total)
    col2.metric("Successful", successful)
    col3.metric("PDFs created", len(generated))

    if successful == total and generated:
        st.success("Done. Your split PDF package is ready to download.")
    elif generated:
        st.warning("Some files may need review. Download the ZIP and check the status table below.")
    else:
        st.error("No output PDFs were created. Check the diagnostics or try a different PDF.")

    st.subheader("Status")
    st.dataframe(simplify_results(results), use_container_width=True, hide_index=True)

    if generated:
        with st.expander("View generated PDF list", expanded=False):
            st.dataframe(generated, use_container_width=True, hide_index=True)

        zip_bytes = zip_folder(output_dir)
        st.download_button(
            "Download split PDF package",
            data=zip_bytes,
            file_name="fdd_split_output.zip",
            mime="application/zip",
            type="primary",
            use_container_width=True,
        )

    if debug:
        with st.expander("Diagnostics log", expanded=True):
            st.code(combined_log.getvalue() or "No diagnostics printed.", language="text")
