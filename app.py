"""
As-Built Drawing Compiler -- HPC HK2794
Streamlit web app -- v4

Features:
- Incremental build: add more ductbook PDFs to the same document
- TQ references per ECS code
- Bundled template + stamp
- Progress bar
- Password protection
"""

import os
import re
import tempfile
import shutil
import base64
from collections import Counter

import streamlit as st

# -- Config --------------------------------------------------------------------
APP_PASSWORD = "HPC2794"

# -- Page config ---------------------------------------------------------------
st.set_page_config(
    page_title="As-Built Compiler -- HK2794",
    page_icon="-",
    layout="centered",
)

# -- Password gate -------------------------------------------------------------
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

if not st.session_state.authenticated:
    st.title("- As-Built Compiler -- HK2794")
    st.markdown("---")
    pwd = st.text_input("Enter password to continue", type="password")
    if st.button("Login", type="primary"):
        if pwd == APP_PASSWORD:
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Incorrect password.")
    st.stop()

# -- Import compiler -----------------------------------------------------------
try:
    from compiler import (
        extract_trn, match_drawings, render_and_stamp,
        build_docx, make_output_filename,
    )
except Exception as e:
    st.error(f"- Failed to load compiler: {e}")
    st.stop()

# -- Session state initialisation ---------------------------------------------
# We persist the build across multiple ductbook uploads using session state.
# session_state keys:
#   trn_data        -- extracted TRN data dict
#   all_matches     -- {ecs: {pdf, page}} accumulated across all batches
#   all_stamped     -- {ecs: png_bytes} accumulated across all batches
#   all_failed      -- [(ecs, reason)] accumulated
#   all_not_found   -- [ecs] still not matched
#   all_duplicates  -- {ecs: [pdfs]}
#   tq_map          -- {ecs: (tq_num, tq_desc)}
#   tmp_dir         -- persistent temp directory for stamped images
#   phase           -- 'upload' | 'tq' | 'done'
#   output_bytes    -- final docx bytes for download
#   output_filename -- filename for download

for key, default in [
    ("trn_data",        None),
    ("all_matches",     {}),
    ("all_stamped",     {}),
    ("all_failed",      []),
    ("all_not_found",   []),
    ("all_duplicates",  {}),
    ("tq_map",          {}),
    ("tmp_dir",         None),
    ("phase",           "upload"),
    ("output_bytes",    None),
    ("output_filename", None),
]:
    if key not in st.session_state:
        st.session_state[key] = default

# -- Helper -- ensure persistent tmp dir exists ---------------------------------
def get_tmp_dir():
    if st.session_state.tmp_dir and os.path.exists(st.session_state.tmp_dir):
        return st.session_state.tmp_dir
    d = tempfile.mkdtemp(prefix="asbuilt_")
    st.session_state.tmp_dir = d
    return d

# -- Reset button --------------------------------------------------------------
def reset_build():
    tmp = st.session_state.get("tmp_dir")
    if tmp and os.path.exists(tmp):
        shutil.rmtree(tmp, ignore_errors=True)
    for key in ["trn_data","all_matches","all_stamped","all_failed",
                "all_not_found","all_duplicates","tq_map","tmp_dir",
                "phase","output_bytes","output_filename"]:
        st.session_state[key] = None if key in ["trn_data","tmp_dir",
                                                  "output_bytes","output_filename"] \
                                 else {} if key in ["all_matches","all_stamped",
                                                    "all_duplicates","tq_map"] \
                                 else [] if key in ["all_failed","all_not_found"] \
                                 else "upload"

# -- Title ---------------------------------------------------------------------
st.title("- As-Built Drawing Compiler")
st.caption("HPC HK2794 - Exentec Hargreaves - Ductwork")
st.markdown("---")

# ------------------------------------------------------------------------------
# PHASE: UPLOAD
# ------------------------------------------------------------------------------
if st.session_state.phase == "upload":

    # If we already have a partial build, show status and allow adding more
    has_partial = bool(st.session_state.all_matches)

    if has_partial:
        trn  = st.session_state.trn_data
        done = len(st.session_state.all_stamped)
        todo = len(st.session_state.all_not_found)
        st.success(
            f"- Build in progress -- **{done} drawings** embedded so far.  "
            f"{'**' + str(todo) + ' still needed.**' if todo else 'All drawings matched!'}"
        )
        if todo:
            missing_db = Counter(
                trn["ecs_ductbook"].get(c, "unknown")
                for c in st.session_state.all_not_found
            )
            st.markdown("**Still need these ductbook PDFs:**")
            for db, cnt in sorted(missing_db.items()):
                st.markdown(f"- `{db}.pdf` -- {cnt} drawings")
        st.markdown("---")

    # -- Upload section --------------------------------------------------------
    if not has_partial:
        st.subheader("1 - Upload files")
    else:
        st.subheader("- Add more drawing PDFs")

    col1, col2 = st.columns(2)

    with col1:
        # Only show TRN uploader if no TRN loaded yet
        if not has_partial:
            trn_file = st.file_uploader(
                "TRN PDF *(required)*", type=["pdf"],
                help="Technical Release Note",
            )
        else:
            trn_file = None
            st.info(f"- TRN: **{st.session_state.trn_data['delivery_ref']}**")

        drawing_files = st.file_uploader(
            "Ductwork Drawing PDFs *(required)*",
            type=["pdf"],
            accept_multiple_files=True,
            help="Upload one or more ductbook PDFs to add to the build.",
        )

    with col2:
        tmp = get_tmp_dir()
        template_saved = os.path.exists(os.path.join(tmp, "template.docx"))
        stamp_saved    = os.path.exists(os.path.join(tmp, "stamp.png"))

        if template_saved:
            st.success("+ Word template uploaded")
            template_file = None
        else:
            template_file = st.file_uploader(
                "As-Built Word template (.docx) *(required)*", type=["docx"]
            )

        if stamp_saved:
            st.success("+ Conformance stamp uploaded")
            stamp_file = None
        else:
            stamp_file = st.file_uploader(
                "Conformance Stamp PNG *(required)*", type=["png"],
                help="Portrait red-border stamp image"
            )

    # File size warning
    if drawing_files:
        total_mb = sum(f.size for f in drawing_files) / 1_048_576
        if total_mb > 80:
            st.error(
                f"-- {total_mb:.0f} MB uploaded -- may exceed memory limit. "
                f"Try 1-2 PDFs at a time."
            )
        elif total_mb > 40:
            st.warning(f"-- {total_mb:.0f} MB uploaded -- may take 3-5 minutes.")

    st.markdown("---")

    # Readiness check
    template_ready = template_file is not None or template_saved
    stamp_ready    = stamp_file    is not None or stamp_saved

    missing = []
    if not has_partial and not trn_file:  missing.append("TRN PDF")
    if not drawing_files:                 missing.append("at least one drawing PDF")
    if not template_ready:                missing.append("Word template")
    if not stamp_ready:                   missing.append("conformance stamp PNG")

    if missing:
        st.info(f"Still needed: {', '.join(missing)}")

    col_btn1, col_btn2 = st.columns([3, 1])
    scan_btn = col_btn1.button(
        "-  Scan Drawing PDFs",
        disabled=bool(missing),
        type="primary",
    )
    if has_partial:
        if col_btn2.button("- Start Over", type="secondary"):
            reset_build()
            st.rerun()

    if scan_btn and not missing:
        status_text  = st.empty()
        progress_bar = st.progress(0)

        def upd(msg, pct):
            status_text.info(f"- {msg}")
            progress_bar.progress(int(pct))

        tmp = get_tmp_dir()

        try:
            upd("Saving files-", 2)

            # Save template
            template_path = os.path.join(tmp, "template.docx")
            if not os.path.exists(template_path):
                with open(template_path, "wb") as f:
                    f.write(template_file.read())

            # Save stamp
            stamp_path = os.path.join(tmp, "stamp.png")
            if not os.path.exists(stamp_path):
                with open(stamp_path, "wb") as f:
                    f.write(stamp_file.read())

            # Extract TRN if not already done
            if not has_partial:
                trn_path = os.path.join(tmp, "trn.pdf")
                with open(trn_path, "wb") as f:
                    f.write(trn_file.read())
                upd("Extracting ECS codes from TRN-", 8)
                st.session_state.trn_data = extract_trn(trn_path)
                if not st.session_state.trn_data["ecs_codes"]:
                    st.error("- No ECS codes found in TRN.")
                    st.stop()

            trn_data     = st.session_state.trn_data
            ecs_codes    = trn_data["ecs_codes"]
            ecs_ductbook = trn_data["ecs_ductbook"]

            upd(f"Found {len(ecs_codes)} ECS codes", 15)

            # Save drawing PDFs
            drawing_paths = []
            for df in drawing_files:
                dp = os.path.join(tmp, df.name)
                with open(dp, "wb") as f:
                    f.write(df.read())
                drawing_paths.append(dp)

            # Match drawings -- only look for codes not yet matched
            already_matched = set(st.session_state.all_matches.keys())
            remaining_codes = [e for e in ecs_codes if e not in already_matched]

            n_pdfs = len(drawing_paths)
            def scan_prog(msg):
                m = re.search(r'\((\d+)/', msg)
                i = int(m.group(1)) if m else 1
                upd(msg, 15 + int(i / n_pdfs * 35))

            new_matches, not_found, new_dupes = match_drawings(
                remaining_codes, ecs_ductbook, drawing_paths,
                progress=scan_prog
            )
            upd(f"Matched {len(new_matches)} new drawings", 50)

            # Merge into session state
            st.session_state.all_matches.update(new_matches)
            st.session_state.all_duplicates.update(new_dupes)

            # not_found = remaining codes still unmatched after this batch
            still_not_found = [e for e in ecs_codes
                               if e not in st.session_state.all_matches]
            st.session_state.all_not_found = still_not_found

            # Render + stamp new matches
            total_new = len(new_matches)
            def stamp_prog(msg):
                m = re.search(r'(\d+)/', msg)
                i = int(m.group(1)) if m else 1
                upd(msg, 50 + int(i / total_new * 40) if total_new else 50)

            new_stamped, new_failed = render_and_stamp(
                new_matches, stamp_path, tmp,
                progress=stamp_prog,
                tq_map=st.session_state.tq_map or None
            )

            # Store paths to stamped images - files live in persistent tmp_dir/stamped
            stamped_dir = os.path.join(tmp, "stamped")
            os.makedirs(stamped_dir, exist_ok=True)
            for ecs, path in new_stamped.items():
                dest = os.path.join(stamped_dir, ecs.replace("-", "_") + ".png")
                import shutil as _sh; _sh.copy2(path, dest)
                st.session_state.all_stamped[ecs] = dest

            st.session_state.all_failed.extend(new_failed)

            upd("Done scanning!", 100)
            progress_bar.progress(100)
            status_text.success(
                f"- Scanned -- {len(new_matches)} new drawings found. "
                f"{'Fill in TQ references below.' if not has_partial else 'Ready to build.'}"
            )

            # Move to TQ phase
            st.session_state.phase = "tq"
            st.rerun()

        except Exception as e:
            status_text.error(f"- Error: {e}")
            import traceback
            st.code(traceback.format_exc())

# ------------------------------------------------------------------------------
# PHASE: TQ + BUILD
# ------------------------------------------------------------------------------
elif st.session_state.phase == "tq":

    trn_data     = st.session_state.trn_data
    ecs_codes    = trn_data["ecs_codes"]
    ecs_ductbook = trn_data["ecs_ductbook"]
    all_matches  = st.session_state.all_matches
    all_stamped  = st.session_state.all_stamped
    not_found    = st.session_state.all_not_found

    total_done = len(all_stamped)
    total_ecs  = len(ecs_codes)

    st.subheader("2b - TQ References (optional)")
    st.caption(
        "For any ECS code affected by a Technical Query, enter the TQ number "
        "and description. Leave blank to skip."
    )

    # Show ECS codes grouped by ductbook
    matched_dbs = []
    for e in ecs_codes:
        db = ecs_ductbook.get(e)
        if db and db not in matched_dbs and e in all_matches:
            matched_dbs.append(db)

    for db in matched_dbs:
        db_codes = [e for e in ecs_codes
                    if ecs_ductbook.get(e) == db and e in all_matches]
        with st.expander(f"{db} -- {len(db_codes)} drawings", expanded=False):
            cols_h = st.columns([3, 2, 4])
            cols_h[0].markdown("**ECS Code**")
            cols_h[1].markdown("**TQ Number**")
            cols_h[2].markdown("**Description**")
            for ecs in db_codes:
                existing_tq = st.session_state.tq_map.get(ecs, ("", ""))
                cols = st.columns([3, 2, 4])
                cols[0].text(ecs)
                tq_num = cols[1].text_input(
                    "TQ", key=f"tq_num_{ecs}",
                    label_visibility="collapsed",
                    placeholder="e.g. TQ137",
                    value=existing_tq[0],
                )
                tq_desc = cols[2].text_input(
                    "Desc", key=f"tq_desc_{ecs}",
                    label_visibility="collapsed",
                    placeholder="Brief description",
                    value=existing_tq[1],
                )
                if tq_num.strip():
                    st.session_state.tq_map[ecs] = (tq_num.strip(), tq_desc.strip())
                elif ecs in st.session_state.tq_map:
                    del st.session_state.tq_map[ecs]

    tq_count = len(st.session_state.tq_map)
    if tq_count:
        st.info(f"-- {tq_count} TQ reference(s) will be applied to drawings.")

    st.markdown("---")
    st.subheader("3 - Build Document")

    # Summary metrics
    col_a, col_b, col_c = st.columns(3)
    col_a.metric("ECS codes in TRN",  total_ecs)
    col_b.metric("Drawings ready",    total_done)
    col_c.metric("Still missing",     len(not_found))

    if not_found:
        missing_db = Counter(ecs_ductbook.get(c, "unknown") for c in not_found)
        lines = "\n".join(
            f"- `{db}.pdf` -- {cnt} drawings"
            for db, cnt in sorted(missing_db.items())
        )
        st.warning(
            f"-- {len(not_found)} drawings not yet found. "
            f"You can build now (partial) or go back and add more PDFs.\n\n{lines}"
        )

    col_b1, col_b2, col_b3 = st.columns([2, 2, 1])
    build_btn   = col_b1.button("-  Build Document Now", type="primary")
    addmore_btn = col_b2.button("- Add More Drawing PDFs")
    restart_btn = col_b3.button("- Start Over")

    if restart_btn:
        reset_build()
        st.rerun()

    if addmore_btn:
        st.session_state.phase = "upload"
        st.rerun()

    if build_btn:
        status_text  = st.empty()
        progress_bar = st.progress(0)

        def upd(msg, pct):
            status_text.info(f"- {msg}")
            progress_bar.progress(int(pct))

        tmp = get_tmp_dir()

        try:
            # Re-apply TQ map -- need to re-render affected drawings
            tq_map = st.session_state.tq_map
            if tq_map:
                upd(f"Applying {len(tq_map)} TQ reference(s) to drawings-", 10)
                stamp_path = os.path.join(tmp, "stamp.png")
                tq_matches = {e: all_matches[e] for e in tq_map if e in all_matches}
                if tq_matches:
                    tq_stamped, _ = render_and_stamp(
                        tq_matches, stamp_path, tmp,
                        tq_map=tq_map
                    )
                    stamped_dir = os.path.join(tmp, "stamped")
                    os.makedirs(stamped_dir, exist_ok=True)
                    for ecs, path in tq_stamped.items():
                        dest = os.path.join(stamped_dir, os.path.basename(path))
                        if path != dest:
                            import shutil as _sh
                            _sh.copy2(path, dest)
                        st.session_state.all_stamped[ecs] = dest

            upd("Building Word document-", 30)

            # Stamped images are already on disk as paths
            stamped_paths = dict(st.session_state.all_stamped)

            template_path   = os.path.join(tmp, "template.docx")
            output_filename = make_output_filename(trn_data)
            output_path     = os.path.join(tmp, output_filename)

            build_docx(
                template_path, trn_data, all_matches,
                stamped_paths, output_path, tmp,
                progress=lambda m: upd(m, 70)
            )

            upd("Done!", 100)
            progress_bar.progress(100)
            status_text.success("- As-Built document built successfully!")

            with open(output_path, "rb") as f:
                st.session_state.output_bytes    = f.read()
            st.session_state.output_filename = output_filename
            st.session_state.phase           = "done"
            st.rerun()

        except Exception as e:
            status_text.error(f"- Error: {e}")
            import traceback
            st.code(traceback.format_exc())

# ------------------------------------------------------------------------------
# PHASE: DONE
# ------------------------------------------------------------------------------
elif st.session_state.phase == "done":

    trn_data     = st.session_state.trn_data
    ecs_codes    = trn_data["ecs_codes"]
    ecs_ductbook = trn_data["ecs_ductbook"]
    all_stamped  = st.session_state.all_stamped
    not_found    = st.session_state.all_not_found
    all_matches  = st.session_state.all_matches

    st.success("- As-Built document ready to download!")
    st.markdown("---")

    # Metrics
    col_a, col_b, col_c = st.columns(3)
    col_a.metric("ECS codes in TRN",  len(ecs_codes))
    col_b.metric("Drawings embedded", len(all_stamped))
    col_c.metric("TQ references",     len(st.session_state.tq_map))

    # Warnings
    if st.session_state.all_duplicates:
        lines = "\n".join(
            f"- `{ecs}` in: {', '.join(Path(p).name for p in pdfs)}"
            for ecs, pdfs in st.session_state.all_duplicates.items()
        )
        st.warning(f"-- {len(st.session_state.all_duplicates)} duplicate ECS codes:\n\n{lines}")

    if not_found:
        missing_db = Counter(ecs_ductbook.get(c, "unknown") for c in not_found)
        lines = "\n".join(
            f"- `{db}.pdf` -- {cnt} drawings"
            for db, cnt in sorted(missing_db.items())
        )
        st.warning(f"-- {len(not_found)} drawings not included:\n\n{lines}")

    if st.session_state.all_failed:
        lines = "\n".join(
            f"- `{ecs}` -- {reason}"
            for ecs, reason in st.session_state.all_failed
        )
        st.error(f"- {len(st.session_state.all_failed)} drawing(s) failed:\n\n{lines}")

    # Appendices summary
    matched_dbs = []
    for e in ecs_codes:
        db = ecs_ductbook.get(e)
        if db and db not in matched_dbs and e in all_stamped:
            matched_dbs.append(db)
    if matched_dbs:
        st.markdown("**Appendices in document:**")
        for i, db in enumerate(matched_dbs):
            n = sum(1 for e in ecs_codes
                    if ecs_ductbook.get(e) == db and e in all_stamped)
            st.markdown(f"- Appendix {i+1}: `{db}` -- {n} drawings")

    st.markdown("---")

    # Download
    st.download_button(
        label=f"--  Download  {st.session_state.output_filename}",
        data=st.session_state.output_bytes,
        file_name=st.session_state.output_filename,
        mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        type="primary",
    )

    st.markdown("")
    col_b1, col_b2 = st.columns(2)

    # Add more drawings -- go back to upload keeping all state
    if col_b1.button("- Add More Drawing PDFs & Rebuild"):
        st.session_state.phase = "upload"
        st.rerun()

    # Start completely fresh
    if col_b2.button("- Start New As-Built"):
        reset_build()
        st.rerun()

# -- Footer --------------------------------------------------------------------
st.markdown("---")
col_f1, col_f2 = st.columns([4, 1])
col_f1.caption("Exentec Hargreaves - HPC HK2794 - As-Built Compiler v4")
if col_f2.button("Logout", use_container_width=True):
    st.session_state.authenticated = False
    st.rerun()
