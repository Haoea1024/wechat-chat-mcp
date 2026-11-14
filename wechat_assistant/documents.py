"""Read common local attachments in bounded, explicitly paged sections."""
import csv
import hashlib
import io
from pathlib import Path
import zipfile
from contextlib import suppress


def _picture(blob, folder, vision):
    from .images import normalized_image
    path=folder/(hashlib.sha256(blob).hexdigest()[:20]+'.png')
    if not path.exists():normalized_image(blob,path)
    return vision(path)


def read_document(path, folder, vision, start=0, count=5):
    path=Path(path);suffix=path.suffix.lower()
    folder.mkdir(parents=True,exist_ok=True)
    parts=[]
    unit='段'
    if suffix in {'.png','.jpg','.jpeg','.webp','.bmp','.gif','.tif','.tiff'}:
        total=1;unit='图片（动画/多帧文件仅首帧）'
        if start==0:parts=[dict(index=0,text=_picture(path.read_bytes(),folder,vision))]
    elif suffix=='.pdf':
        from pypdf import PdfReader
        import pypdfium2 as pdfium
        pdf=PdfReader(path);total=len(pdf.pages);unit='页'
        with pdfium.PdfDocument(str(path)) as rendered:
            for i in range(start,min(start+count,total)):
                page=pdf.pages[i];text=page.extract_text() or ''
                # Also read diagrams/screenshots; text-only PDF extraction would omit them.
                rich=bool(page.images) or len(text.strip())<30
                if rich:
                    image=folder/f'pdf-{i+1}.png'
                    rendered[i].render(scale=1.6).to_pil().save(image)
                    visual=vision(image)
                    text=(text+'\n[页面识读]\n'+visual).strip()
                parts.append(dict(index=i,text=f'第 {i+1} 页\n{text}'))
    elif suffix=='.docx':
        from docx import Document
        from docx.table import Table
        doc=Document(path);blocks=[]
        for block in doc.iter_inner_content():
            blocks.append('\n'.join('\t'.join(c.text for c in row.cells) for row in block.rows) if isinstance(block,Table) else block.text)
        chunks=['\n'.join(blocks[i:i+50]) for i in range(0,len(blocks),50)]
        pictures=[r.target_part.blob for r in doc.part.rels.values() if not r.is_external and r.reltype.endswith('/image')]
        total=len(chunks)+len(pictures);unit='段/插图'
        for i in range(start,min(start+count,total)):
            text=chunks[i] if i<len(chunks) else '[文档插图]\n'+_picture(pictures[i-len(chunks)],folder,vision)
            parts.append(dict(index=i,text=text))
    elif suffix=='.pptx':
        from pptx import Presentation
        presentation=Presentation(path);total=len(presentation.slides);unit='幻灯片'
        for i in range(start,min(start+count,total)):
            slide=presentation.slides[i];pieces=[]
            def shapes_text(shapes):
                for shape in shapes:
                    if hasattr(shape,'shapes'):shapes_text(shape.shapes)
                    if shape.has_text_frame:pieces.append(shape.text)
                    if shape.has_table:pieces.extend('\t'.join(c.text for c in row.cells) for row in shape.table.rows)
                    if shape.shape_type==13:pieces.append('[插图]\n'+_picture(shape.image.blob,folder,vision))
            shapes_text(slide.shapes)
            if slide.has_notes_slide:pieces.append('[备注] '+slide.notes_slide.notes_text_frame.text)
            parts.append(dict(index=i,text=f'幻灯片 {i+1}\n'+'\n'.join(pieces)))
    elif suffix in {'.xlsx','.xls','.csv'}:
        sheets=[]
        if suffix=='.xlsx':
            from openpyxl import load_workbook
            book=load_workbook(path,read_only=True,data_only=False)
            try:
                sheets=[(sheet.title,list(sheet.values)) for sheet in book]
            finally:book.close()
        elif suffix=='.xls':
            import xlrd
            book=xlrd.open_workbook(path)
            sheets=[(s.name,[s.row_values(i) for i in range(s.nrows)]) for s in book.sheets()]
            book.release_resources()
        else:
            text=read_text(path)
            sheets=[(path.name,list(csv.reader(io.StringIO(text))))]
        chunks=[]
        for title,rows in sheets:
            for offset in range(0,len(rows),150):
                chunks.append(f'工作表 {title}，第 {offset+1}–{min(offset+150,len(rows))} 行\n'+'\n'.join('\t'.join('' if v is None else str(v) for v in row) for row in rows[offset:offset+150]))
        total=len(chunks);unit='表格片段（公式按原文保留）'
        parts=[dict(index=i,text=chunks[i]) for i in range(start,min(start+count,total))]
    elif suffix in {'.doc','.ppt'}:
        # Convert a read-only copy through the locally installed Office/WPS COM server.
        import pythoncom
        import win32com.client.dynamic
        import pywintypes
        pythoncom.CoInitialize()
        app=None;doc=None
        destination=folder/(path.stem+('x.docx' if suffix=='.doc' else 'x.pptx'))
        try:
            dispatch=pythoncom.CoCreateInstance('Word.Application' if suffix=='.doc' else 'PowerPoint.Application',None,pythoncom.CLSCTX_LOCAL_SERVER,pythoncom.IID_IDispatch)
            app=win32com.client.dynamic.Dispatch(dispatch)
            app.AutomationSecurity=3
            if suffix=='.doc':
                app.Visible=False;app.DisplayAlerts=0
                doc=app.Documents.Open(str(path),ReadOnly=True,AddToRecentFiles=False)
                doc.SaveAs2(str(destination),FileFormat=16)
            else:
                doc=app.Presentations.Open(str(path),ReadOnly=True,Untitled=False,WithWindow=False)
                doc.SaveAs(str(destination),24)
        finally:
            if doc is not None:
                with suppress(pywintypes.com_error):doc.Close()
            if app is not None:
                with suppress(pywintypes.com_error):app.Quit()
            pythoncom.CoUninitialize()
        return read_document(destination,folder,vision,start,count)
    elif suffix=='.zip':
        with zipfile.ZipFile(path) as archive:
            names=[i.filename for i in archive.infolist() if not i.is_dir()]
        total=len(names);unit='压缩包成员（尚未读取成员正文）'
        parts=[dict(index=i,text=names[i]) for i in range(start,min(start+count,total))]
    elif suffix in {'.txt','.md','.rst','.json','.xml','.html','.htm','.py','.js','.ts','.yaml','.yml','.log'}:
        text=read_text(path);chunks=[text[i:i+8000] for i in range(0,len(text),8000)]
        total=len(chunks);parts=[dict(index=i,text=chunks[i]) for i in range(start,min(start+count,total))]
    else:
        raise ValueError(f'暂不解析 {suffix or "无扩展名"} 文件；不会执行附件。')
    return dict(parts=parts,total_units=total,unit=unit,start=start,next_start=start+len(parts) if start+len(parts)<total else None)


def read_text(path):
    data=Path(path).read_bytes()
    for encoding in ('utf-8-sig','gb18030'):
        try:return data.decode(encoding)
        except UnicodeDecodeError:pass
    raise ValueError('文本编码无法识别。')
