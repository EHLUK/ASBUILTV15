"""
As-Built Drawing Compiler -- HPC HK2794
Core logic.

Improvements in this version:
- Drawings output in TRN order (not dict order)
- Duplicate ECS code detection and reporting
- Error recovery -- bad pages are skipped, not fatal
- Clean output filename generation
- Watermark-bleed cell parsing (splits on newlines)
- Correct area code extraction (H-prefix only)
- sectPr always has headerReference/footerReference
- ECS table regex handles self-closing tags with spaces
"""

import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from collections import Counter
from pathlib import Path

import pdfplumber
from PIL import Image


# -- Patterns ------------------------------------------------------------------
ECS_PATTERN  = re.compile(r'^\d[A-Z]{2,4}\d{4}-\d{2}-\d+$')
DB_PATTERN   = re.compile(r'ENG-GSC-WS08-\d+')
TC_REF_PAT   = re.compile(r'TC Ref[:\s]+(\d{6,})')
DELIVERY_PAT = re.compile(r'S\.S Welded.*?DO[\d,\s&]+\d+')
AREA_PAT     = re.compile(r'\b(H[A-Z]{2}-\d{2})\b')  # HKX-08, HVL-03 etc

DUCTBOOK_INFO = {
    'ENG-GSC-WS08-02385': ('5.6.2 Ductwork Book HKX-08 EL ]+14,80m;+19,50m]', '01'),
    'ENG-GSC-WS08-02393': ('5.6.2 Ductwork Book HKX-08 EL ]+14,80m;+19,50m]', '01'),
    'ENG-GSC-WS08-02978': ('5.6.2 Ductwork Book HWX-05', '02'),
    'ENG-GSC-WS08-03005': ('5.6.2 Ductwork Book HVL-03', '02'),
    'ENG-GSC-WS08-03046': ('5.6.2 Ductwork Book HWX-04', '02'),
}

# Image sizing -- calibrated from LibreOffice (15.24 cm - 21.56 cm)
FIXED_CX = 5_486_400
FIXED_CY = 7_761_600


# -- Step 1 -- TRN extraction ---------------------------------------------------

def extract_trn(trn_path: str, progress=None) -> dict:
    """
    Extract ECS codes, ductbook groups and header fields from TRN PDF.
    Returns dict with keys: ecs_codes, ecs_ductbook, delivery_ref, tc_ref, area_codes.
    ecs_codes is ordered to match TRN table order.
    """
    ecs_codes    = []   # ordered
    ecs_ductbook = {}
    seen         = set()
    delivery_ref = ""
    tc_ref       = ""
    area_codes   = []

    if progress:
        progress("Reading TRN PDF-")

    with pdfplumber.open(trn_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""

            if not delivery_ref:
                m = DELIVERY_PAT.search(text)
                if m:
                    delivery_ref = m.group(0).strip()
            if not tc_ref:
                m = TC_REF_PAT.search(text)
                if m:
                    tc_ref = m.group(1)
            if not area_codes:
                found = AREA_PAT.findall(text)
                area_codes = list(dict.fromkeys(found))

            for table in (page.extract_tables() or []):
                for row in table:
                    if not row:
                        continue
                    row_ecs, row_db = [], None
                    for cell in row:
                        if not cell:
                            continue
                        # Split on newlines -- watermark bleed prefixes cells (e.g. "A\n1DWK...")
                        for part in str(cell).strip().split('\n'):
                            part = part.strip()
                            if not part:
                                continue
                            if ECS_PATTERN.match(part) and part not in seen:
                                row_ecs.append(part)
                                seen.add(part)
                            db_m = DB_PATTERN.search(part)
                            if db_m:
                                row_db = db_m.group(0)
                    for ecs in row_ecs:
                        ecs_codes.append(ecs)
                        if row_db:
                            ecs_ductbook[ecs] = row_db

    return {
        "ecs_codes":    ecs_codes,
        "ecs_ductbook": ecs_ductbook,
        "delivery_ref": delivery_ref,
        "tc_ref":       tc_ref,
        "area_codes":   area_codes,
    }


# -- Step 2 -- Drawing matching -------------------------------------------------

def _scan_pdf(pdf_path: str, ecs_set: set) -> dict:
    """
    Scan one drawing PDF using the list sheet for speed.
    The list sheet (first page with 5+ piece numbers) maps each piece to its page.
    Falls back to full pdfplumber scan if no list sheet found.
    Returns {ecs: [(page_num, ecs_count_on_page), ...]}
    """
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(pdf_path)
        try:
            # Find list sheet in first 10 pages
            for i in range(min(10, len(pdf))):
                text     = pdf[i].get_textpage().get_text_range()
                ecs_like = re.findall(r'-DW[A-Z]\d{4}-\d{2}-\d+', text)
                if len(ecs_like) > 5:
                    # Parse: each entry is "-DWKxxxx-xx-xx PAGE_NUM"
                    results = {}
                    tokens  = text.split()
                    j = 0
                    while j < len(tokens):
                        w = tokens[j]
                        if re.match(r'^-DW[A-Z]\d{4}-\d{2}-\d+$', w):
                            ecs = '1' + w[1:]
                            if j+1 < len(tokens) and tokens[j+1].isdigit():
                                page_num = int(tokens[j+1]) - 1
                                if ecs in ecs_set:
                                    results[ecs] = [(page_num, 1)]
                            j += 2
                        else:
                            j += 1
                    if results:
                        return results
        finally:
            pdf.close()
    except Exception:
        pass  # fall back to pdfplumber

    # Fallback: full pdfplumber scan
    results = {}
    with pdfplumber.open(pdf_path) as pdf:
        for page_num, page in enumerate(pdf.pages):
            ecs_on_page = []
            for w in page.extract_words():
                txt = w['text'].strip()
                if ECS_PATTERN.match(txt) and txt in ecs_set:
                    ecs_on_page.append(txt)
                if re.match(r'^-DW[A-Z]\d{4}-\d{2}-\d+$', txt):
                    candidate = '1' + txt[1:]
                    if candidate in ecs_set:
                        ecs_on_page.append(candidate)
            for ecs in set(ecs_on_page):
                results.setdefault(ecs, []).append((page_num, len(set(ecs_on_page))))
    return results


def match_drawings(ecs_codes: list, ecs_ductbook: dict,
                   drawing_pdf_paths: list, progress=None):
    """
    Match each ECS code to its individual drawing page.
    Returns (matches, not_found, duplicates).
      matches    = {ecs: {"pdf": path, "page": int}}
      not_found  = [ecs, ...]
      duplicates = {ecs: [pdf1, pdf2, ...]}  -- same ECS found in multiple PDFs
    """
    ecs_set = set(ecs_codes)
    ecs_page_candidates = {}

    for i, pdf_path in enumerate(drawing_pdf_paths):
        if progress:
            progress(f"Scanning {Path(pdf_path).name} ({i+1}/{len(drawing_pdf_paths)})-")
        for ecs, hits in _scan_pdf(pdf_path, ecs_set).items():
            for page_num, count in hits:
                ecs_page_candidates.setdefault(ecs, []).append((page_num, count, pdf_path))
        import gc; gc.collect()  # free memory after each PDF

    matches    = {}
    duplicates = {}

    for ecs, candidates in ecs_page_candidates.items():
        # Individual drawing page = minimum ECS count on page
        best = min(candidates, key=lambda x: x[1])
        matches[ecs] = {"pdf": best[2], "page": best[0]}
        # Flag if same ECS appeared in more than one PDF
        pdfs = list(dict.fromkeys(c[2] for c in candidates))
        if len(pdfs) > 1:
            duplicates[ecs] = pdfs

    not_found = [c for c in ecs_codes if c not in matches]
    return matches, not_found, duplicates



# -- TQ box rendering ----------------------------------------------------------

def _apply_tq_box(img: Image.Image, tq_number: str, tq_desc: str) -> Image.Image:
    """
    Apply a rotated red-bordered TQ box onto a stamped drawing image.
    Position calibrated at 120dpi portrait (1404x1985px).
    """
    from PIL import ImageDraw, ImageFont

    BOX_W, BOX_H = 420, 130
    TQ_X, TQ_Y   = 888, 1483   # calibrated position

    try:
        font_bold = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 18)
        font_reg = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 15)
    except Exception:
        font_bold = ImageFont.load_default()
        font_reg  = ImageFont.load_default()

    # Build box on white canvas
    tq_img  = Image.new('RGBA', (BOX_W, BOX_H), (255, 255, 255, 255))
    tq_draw = ImageDraw.Draw(tq_img)
    tq_draw.rectangle([0, 0, BOX_W - 1, BOX_H - 1], outline='red', width=3)
    tq_draw.text((10, 8),  f"{tq_number}:", font=font_bold, fill='red')

    # Word-wrap description to fit box width
    words     = tq_desc.split()
    lines, line = [], ""
    for word in words:
        test = (line + " " + word).strip()
        bbox = tq_draw.textbbox((0, 0), test, font=font_reg)
        if bbox[2] - bbox[0] > BOX_W - 20 and line:
            lines.append(line)
            line = word
        else:
            line = test
    if line:
        lines.append(line)
    tq_draw.multiline_text((10, 34), "\n".join(lines),
                           font=font_reg, fill='black', spacing=4)

    # Rotate 90- clockwise and paste onto drawing
    tq_rotated = tq_img.rotate(-90, expand=True)
    img = img.convert('RGBA')
    img.paste(tq_rotated, (TQ_X, TQ_Y), tq_rotated)
    return img


# -- Step 3+4 -- Render + stamp -------------------------------------------------

def render_and_stamp(matches: dict, stamp_path: str,
                     work_dir: str, progress=None,
                     tq_map: dict = None) -> dict:
    """
    Render each matched page with pdftoppm, rotate 90- CW, apply stamp.
    Skips pages that fail to render rather than crashing the whole job.
    Returns {ecs: stamped_png_path}
    """
    images_dir  = os.path.join(work_dir, "images")
    stamped_dir = os.path.join(work_dir, "stamped")
    os.makedirs(images_dir,  exist_ok=True)
    os.makedirs(stamped_dir, exist_ok=True)

    stamp_src = Image.open(stamp_path).convert("RGBA")
    STAMP_W   = 170
    STAMP_H   = int(stamp_src.height * STAMP_W / stamp_src.width)
    stamp_fit = stamp_src.resize((STAMP_W, STAMP_H), Image.LANCZOS)

    CELL_X1, CELL_Y1 = 350, 1283
    CELL_X2           = 750
    STAMP_X = CELL_X1 + (CELL_X2 - CELL_X1 - STAMP_W) // 2 + 71
    STAMP_Y = CELL_Y1 - 37

    stamped_paths = {}
    failed        = []
    total         = len(matches)

    for i, (ecs, info) in enumerate(matches.items()):
        if progress:
            progress(f"Rendering & stamping {i+1}/{total}: {ecs}-")
        try:
            out_prefix = os.path.join(images_dir, ecs)
            subprocess.run([
                "pdftoppm", "-r", "120", "-jpeg",
                "-f", str(info["page"] + 1),
                "-l", str(info["page"] + 1),
                info["pdf"], out_prefix,
            ], check=True, capture_output=True, timeout=60)

            candidates = sorted([
                f for f in os.listdir(images_dir)
                if f.startswith(os.path.basename(out_prefix)) and f.endswith(".jpg")
            ])
            if not candidates:
                failed.append((ecs, "pdftoppm produced no output"))
                continue

            img = Image.open(os.path.join(images_dir, candidates[-1]))
            img = img.rotate(-90, expand=True).convert("RGBA")
            img.paste(stamp_fit, (STAMP_X, STAMP_Y), stamp_fit)
            out = os.path.join(stamped_dir, f"{ecs}.png")
            # Apply TQ box if this ECS code has one
            if tq_map and ecs in tq_map:
                tq_num, tq_desc = tq_map[ecs]
                img = _apply_tq_box(img, tq_num, tq_desc)

            img.convert("RGB").save(out, "PNG")
            stamped_paths[ecs] = out
            del img  # free memory immediately
            import gc; gc.collect()

        except Exception as e:
            failed.append((ecs, str(e)))

    if failed:
        print(f"Warning: {len(failed)} drawings failed to render: {failed}")

    return stamped_paths, failed


# -- Step 5 -- Word document build ----------------------------------------------

def _fit_image(img_w, img_h):
    cx = FIXED_CX
    cy = int(cx * img_h / img_w)
    if cy > FIXED_CY:
        cy = FIXED_CY
        cx = int(cy * img_w / img_h)
    return cx, cy


def _img_para(rid, ecs, img_path):
    img      = Image.open(img_path)
    cx, cy   = _fit_image(img.size[0], img.size[1])
    uid      = abs(hash(ecs)) % 999990 + 1
    return f"""    <w:p>
      <w:pPr><w:pageBreakBefore w:val="1"/><w:spacing w:before="0" w:after="0" w:line="240" w:lineRule="auto"/><w:jc w:val="center"/></w:pPr>
      <w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0"
        xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">
        <wp:extent cx="{cx}" cy="{cy}"/>
        <wp:effectExtent l="0" t="0" r="0" b="0"/>
        <wp:docPr id="{uid}" name="{ecs}"/>
        <wp:cNvGraphicFramePr><a:graphicFrameLocks xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" noChangeAspect="1"/></wp:cNvGraphicFramePr>
        <a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
          <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
            <pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">
              <pic:nvPicPr><pic:cNvPr id="0" name="{ecs}"/><pic:cNvPicPr><a:picLocks noChangeAspect="1" noChangeArrowheads="1"/></pic:cNvPicPr></pic:nvPicPr>
              <pic:blipFill><a:blip r:embed="{rid}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>
              <pic:spPr bwMode="auto"><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom><a:noFill/></pic:spPr>
            </pic:pic>
          </a:graphicData>
        </a:graphic>
      </wp:inline></w:drawing></w:r>
    </w:p>"""


def build_docx(template_path: str, trn_data: dict, matches: dict,
               stamped_paths: dict, output_path: str, work_dir: str,
               progress=None) -> str:
    """Build the completed As-Built Word document. Returns output_path."""

    ecs_codes    = trn_data["ecs_codes"]   # TRN order preserved
    ecs_ductbook = trn_data["ecs_ductbook"]
    delivery_ref = trn_data["delivery_ref"]
    tc_ref       = trn_data["tc_ref"]
    area_codes   = trn_data["area_codes"]

    docx_work = os.path.join(work_dir, "docx_work")
    if os.path.exists(docx_work):
        shutil.rmtree(docx_work)
    os.makedirs(docx_work)

    if progress:
        progress("Unpacking Word template-")

    with zipfile.ZipFile(template_path, 'r') as z:
        z.extractall(docx_work)

    doc_xml_path = os.path.join(docx_work, "word", "document.xml")
    rels_path    = os.path.join(docx_work, "word", "_rels", "document.xml.rels")
    ct_path      = os.path.join(docx_work, "[Content_Types].xml")
    media_dir    = os.path.join(docx_work, "word", "media")
    os.makedirs(media_dir, exist_ok=True)

    with open(doc_xml_path, 'r', encoding='utf-8') as f:
        xml = f.read()

    # -- 5a Text replacements -------------------------------------------------
    if progress:
        progress("Filling in header fields-")

    area_str = " / ".join(dict.fromkeys(area_codes)) if area_codes else ""
    do_str   = delivery_ref.replace("S.S Welded - ", "").replace("&", "&amp;") \
               if delivery_ref else "DO??"

    matched_dbs = []
    for ecs in ecs_codes:
        db = ecs_ductbook.get(ecs)
        if db and db not in matched_dbs and ecs in stamped_paths:
            matched_dbs.append(db)

    first_db  = matched_dbs[0] if matched_dbs else "ENG-GSC-WS08-XXXXX"
    _, first_rev = DUCTBOOK_INFO.get(first_db, (first_db, "01"))

    replacements = [
        ('HK2794 As-Built Drawings Area XXXXXX',  area_str),
        ('HK2794 As-Built Drawings Area XXXXXXX', area_str),
        ('Ductwork Book Delivery Order XX',        f'DO {do_str}'),
        # Handle case where XX is in a separate XML run
        ('Delivery Order </w:t></w:r>',            f'Delivery Order {do_str}</w:t></w:r>'),
        ('XXXXXXXX',                               tc_ref or "????????"),
        ('Stainless Steel Welded Duct Delivery Order XX associated',
         f'Stainless Steel Welded Duct Delivery Order {do_str} associated'),
        ('HK2794 ITP for Stainless Steel Welded Duct D0XX',
         f'HK2794 ITP for Stainless Steel Welded Duct {do_str}'),
        (' (TC number XXXXXXX). ', f' (TC number: {tc_ref or "___________"}). '),
        ('APPENDIX 1 - DUCTWORK BOOK: ENG-GSC-WS08-0XXX (Rev 0X)',
         f'APPENDIX 1 - DUCTWORK BOOK: {first_db} (Rev {first_rev})'),
    ]
    for old, new in replacements:
        xml = xml.replace(old, new)

    # -- 5b Ductwork Books table -----------------------------------------------
    def make_db_row(db, idx):
        desc, rev = DUCTBOOK_INFO.get(db, (db, '01'))
        pids = [f'0A{idx+j:06X}' for j in range(4)]
        return (
            f'<w:tr w14:paraId="{pids[0]}" w14:textId="FFFFFFFF" '
            f'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">'
            f'<w:tc><w:tcPr><w:tcW w:w="1418" w:type="dxa"/></w:tcPr>'
            f'<w:p w14:paraId="{pids[1]}" xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">'
            f'<w:r><w:t>{db}</w:t></w:r></w:p></w:tc>'
            f'<w:tc><w:tcPr><w:tcW w:w="4513" w:type="dxa"/></w:tcPr>'
            f'<w:p w14:paraId="{pids[2]}" xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">'
            f'<w:r><w:t>{desc}</w:t></w:r></w:p></w:tc>'
            f'<w:tc><w:tcPr><w:tcW w:w="851" w:type="dxa"/></w:tcPr>'
            f'<w:p w14:paraId="{pids[3]}" xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">'
            f'<w:r><w:t>{rev}</w:t></w:r></w:p></w:tc>'
            f'</w:tr>'
        )

    new_rows = ''.join(make_db_row(db, 0x100 + i*10) for i, db in enumerate(matched_dbs))
    m = re.search(r'w14:paraId="30481C10"', xml)
    if m:
        tr_start = xml.rfind('<w:tr ', 0, m.start())
        tr_end   = xml.find('</w:tr>', m.start()) + len('</w:tr>')
        xml      = xml[:tr_start] + new_rows + xml[tr_end:]

    # -- 5c ECS code table -- in TRN order -------------------------------------
    para_idx = 0x200

    def make_ecs_tables():
        nonlocal para_idx
        tables = []
        for db in matched_dbs:
            # Use TRN order (ecs_codes is ordered as per TRN)
            db_codes = [e for e in ecs_codes
                        if ecs_ductbook.get(e) == db and e in stamped_paths]
            if not db_codes:
                continue
            hpid     = f'0B{para_idx:06X}'; para_idx += 1
            rows_xml = (
                f'<w:tr><w:tc><w:tcPr><w:gridSpan w:val="4"/>'
                f'<w:shd w:val="clear" w:color="auto" w:fill="D9D9D9"/></w:tcPr>'
                f'<w:p w14:paraId="{hpid}" xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">'
                f'<w:pPr><w:jc w:val="center"/></w:pPr>'
                f'<w:r><w:rPr><w:b/></w:rPr><w:t>{db}</w:t></w:r></w:p></w:tc></w:tr>'
            )
            for i in range(0, len(db_codes), 4):
                chunk = db_codes[i:i+4]
                while len(chunk) < 4:
                    chunk.append('')
                rpid  = f'0B{para_idx:06X}'; para_idx += 1
                cells = ''
                for code in chunk:
                    cp = f'0B{para_idx:06X}'; para_idx += 1
                    cells += (
                        f'<w:tc><w:tcPr><w:tcW w:w="2550" w:type="dxa"/></w:tcPr>'
                        f'<w:p w14:paraId="{cp}" xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">'
                        f'<w:r><w:t>{code}</w:t></w:r></w:p></w:tc>'
                    )
                rows_xml += (
                    f'<w:tr w14:paraId="{rpid}" '
                    f'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml">'
                    f'{cells}</w:tr>'
                )
            tables.append(
                f'<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/>'
                f'<w:tblW w:w="0" w:type="auto"/></w:tblPr>'
                f'<w:tblGrid><w:gridCol w:w="2550"/><w:gridCol w:w="2550"/>'
                f'<w:gridCol w:w="2550"/><w:gridCol w:w="2551"/></w:tblGrid>'
                f'{rows_xml}</w:tbl>'
            )
        return '\n'.join(tables)

    # Self-closing tags may have a space before /> -- handle both forms
    m2 = re.search(
        r'<w:tblGrid>\s*<w:gridCol w:w="2550"\s*/>\s*<w:gridCol w:w="2550"\s*/>'
        r'\s*<w:gridCol w:w="2550"\s*/>\s*<w:gridCol w:w="2551"\s*/>', xml)
    if m2:
        tbl_start = xml.rfind('<w:tbl>', 0, m2.start())
        tbl_end   = xml.find('</w:tbl>', m2.start()) + len('</w:tbl>')
        xml       = xml[:tbl_start] + make_ecs_tables() + xml[tbl_end:]

    # -- 5d Register images + build appendices (TRN order) --------------------
    if progress:
        progress("Embedding drawings into document-")

    with open(rels_path, 'r', encoding='utf-8') as f:
        rels = f.read()

    rId_map = {}
    rId_num = 100
    for ecs, img_path in stamped_paths.items():
        ecs_safe = ecs.replace('-', '_')
        shutil.copy2(img_path, os.path.join(media_dir, f"drawing_{ecs_safe}.png"))
        rid = f'rId{rId_num}'
        rId_map[ecs] = rid
        if rid not in rels:
            rels = rels.replace('</Relationships>',
                f'<Relationship Id="{rid}" '
                f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                f'Target="media/drawing_{ecs_safe}.png"/>\n</Relationships>')
        rId_num += 1

    with open(rels_path, 'w', encoding='utf-8') as f:
        f.write(rels)

    with open(ct_path, 'r', encoding='utf-8') as f:
        ct = f.read()
    if 'Extension="png"' not in ct:
        ct = ct.replace('</Types>',
            '<Default Extension="png" ContentType="image/png"/>\n</Types>')
        with open(ct_path, 'w', encoding='utf-8') as f:
            f.write(ct)

    # Build appendix XML -- drawings in TRN order
    appendix_xml = ''
    for i, db in enumerate(matched_dbs):
        db_codes = [e for e in ecs_codes
                    if ecs_ductbook.get(e) == db and e in stamped_paths]
        if not db_codes:
            continue
        _, rev = DUCTBOOK_INFO.get(db, (db, '01'))
        appendix_xml += (
            f'<w:p><w:pPr><w:pStyle w:val="Heading1"/>'
            f'<w:pageBreakBefore w:val="1"/></w:pPr>'
            f'<w:r><w:t>APPENDIX {i+1} - DUCTWORK BOOK: {db} (Rev {rev})</w:t></w:r></w:p>'
        )
        for ecs in db_codes:
            appendix_xml += _img_para(rId_map[ecs], ecs, stamped_paths[ecs])

    inject_point = xml.rfind('</w:body>')
    xml = xml[:inject_point] + appendix_xml + xml[inject_point:]

    # -- Ensure sectPr is last child and has header/footer refs ---------------
    all_sect = list(re.finditer(r'<w:sectPr[ >].*?</w:sectPr>', xml, re.DOTALL))
    if all_sect:
        sp_match = all_sect[-1]
        sect_pr  = sp_match.group(0)
        xml      = xml[:sp_match.start()] + xml[sp_match.end():]
        # Always inject -- the last sectPr in this template lacks them
        if 'headerReference' not in sect_pr:
            sect_pr = sect_pr.replace('<w:pgSz',
                '<w:headerReference w:type="default" r:id="rId8"/>\n'
                '  <w:footerReference w:type="default" r:id="rId9"/>\n  <w:pgSz')
        body_end = xml.rfind('</w:body>')
        xml = xml[:body_end] + '\n  ' + sect_pr + '\n' + xml[body_end:]

    with open(doc_xml_path, 'w', encoding='utf-8') as f:
        f.write(xml)

    # -- Repack ----------------------------------------------------------------
    if progress:
        progress("Packing final Word document-")

    with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(docx_work):
            for file in files:
                file_path = os.path.join(root, file)
                zf.write(file_path, os.path.relpath(file_path, docx_work))

    return output_path


# -- Filename helper -----------------------------------------------------------

def make_output_filename(trn_data: dict) -> str:
    """Generate a clean output filename from TRN data."""
    tc  = trn_data.get("tc_ref", "")
    ref = trn_data.get("delivery_ref", "")
    # Extract DO numbers e.g. "DO24, 27, 30 & 31" -> "DO24-27-30-31"
    do_nums = re.findall(r'\d+', ref.split("DO", 1)[-1]) if "DO" in ref else []
    do_part = "DO" + "-".join(do_nums) if do_nums else "AsBuilt"
    tc_part = f"_TC{tc}" if tc else ""
    return f"AsBuilt_{do_part}{tc_part}.docx"
