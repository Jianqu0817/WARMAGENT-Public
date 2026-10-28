import json
import zipfile

from warmagent.document_tools import read_document


def test_docx_and_unicode_text_are_paged_with_original_hash(tmp_path):
    doc = tmp_path / 'fixture.docx'
    text = '中文文档内容与来源。' * 400
    with zipfile.ZipFile(doc, 'w') as archive:
        archive.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:body><w:p><w:r><w:t>' + text + '</w:t></w:r></w:p></w:body></w:document>')
    offset, pages = 0, []
    while True:
        result = read_document(str(doc), offset=offset)
        assert len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()) < 5800
        assert result['content_kind'] == 'untrusted_document_data' and result['sha256']
        pages.append(result['text'])
        if result['next_offset'] is None:
            break
        offset = result['next_offset']
    assert ''.join(pages) == text


def test_pdf_reports_page_count_and_does_not_invent_image_text(tmp_path):
    import pytest
    pypdf = pytest.importorskip('pypdf')
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=120, height=120)
    file = tmp_path / 'blank.pdf'
    with file.open('wb') as stream:
        writer.write(stream)
    assert read_document(file)['pages'] == 1
    result = read_document(file, page=1)
    assert result['text'] == '' and '扫描图片' in result['limitations']
