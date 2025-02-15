import logging
from io import BytesIO
from pathlib import Path, PurePath
from typing import ClassVar, Dict, Iterable, List, Optional, Type, Union

from docling_core.types import BaseCell, BaseText
from docling_core.types import BoundingBox as DsBoundingBox
from docling_core.types import Document as DsDocument
from docling_core.types import DocumentDescription as DsDocumentDescription
from docling_core.types import FileInfoObject as DsFileInfoObject
from docling_core.types import PageDimensions, PageReference, Prov, Ref
from docling_core.types import Table as DsSchemaTable
from docling_core.types import TableCell
from pydantic import BaseModel

from docling.backend.abstract_backend import PdfDocumentBackend
from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
from docling.datamodel.base_models import (
    AssembledUnit,
    ConversionStatus,
    DocumentStream,
    FigureElement,
    Page,
    TableElement,
    TextElement,
)
from docling.datamodel.settings import DocumentLimits
from docling.utils.utils import create_file_hash

_log = logging.getLogger(__name__)

layout_label_to_ds_type = {
    "Title": "title",
    "Document Index": "table-of-path_or_stream",
    "Section-header": "subtitle-level-1",
    "Checkbox-Selected": "checkbox-selected",
    "Checkbox-Unselected": "checkbox-unselected",
    "Caption": "caption",
    "Page-header": "page-header",
    "Page-footer": "page-footer",
    "Footnote": "footnote",
    "Table": "table",
    "Formula": "equation",
    "List-item": "paragraph",
    "Code": "paragraph",
    "Picture": "figure",
    "Text": "paragraph",
}


class InputDocument(BaseModel):
    file: PurePath = None
    document_hash: Optional[str] = None
    valid: bool = False
    limits: DocumentLimits = DocumentLimits()

    filesize: Optional[int] = None
    page_count: Optional[int] = None

    _backend: PdfDocumentBackend = None  # Internal PDF backend used

    def __init__(
        self,
        path_or_stream: Union[BytesIO, Path],
        filename: Optional[str] = None,
        limits: Optional[DocumentLimits] = None,
        pdf_backend=PyPdfiumDocumentBackend,
    ):
        super().__init__()

        self.limits = limits or DocumentLimits()

        try:
            if isinstance(path_or_stream, Path):
                self.file = path_or_stream
                self.filesize = path_or_stream.stat().st_size
                if self.filesize > self.limits.max_file_size:
                    self.valid = False
                else:
                    self.document_hash = create_file_hash(path_or_stream)
                    self._backend = pdf_backend(path_or_stream=path_or_stream)

            elif isinstance(path_or_stream, BytesIO):
                self.file = PurePath(filename)
                self.filesize = path_or_stream.getbuffer().nbytes

                if self.filesize > self.limits.max_file_size:
                    self.valid = False
                else:
                    self.document_hash = create_file_hash(path_or_stream)
                    self._backend = pdf_backend(path_or_stream=path_or_stream)

            if self.document_hash and self._backend.page_count() > 0:
                self.page_count = self._backend.page_count()

                if self.page_count <= self.limits.max_num_pages:
                    self.valid = True

        except (FileNotFoundError, OSError) as e:
            _log.exception(
                f"File {self.file.name} not found or cannot be opened.", exc_info=e
            )
            # raise
        except RuntimeError as e:
            _log.exception(
                f"An unexpected error occurred while opening the document {self.file.name}",
                exc_info=e,
            )
            # raise


class ConvertedDocument(BaseModel):
    input: InputDocument

    status: ConversionStatus = ConversionStatus.PENDING  # failure, success
    errors: List[Dict] = []  # structure to keep errors

    pages: List[Page] = []
    assembled: Optional[AssembledUnit] = None

    output: Optional[DsDocument] = None

    def to_ds_document(self) -> DsDocument:
        title = ""
        desc = DsDocumentDescription(logs=[])

        page_hashes = [
            PageReference(hash=p.page_hash, page=p.page_no, model="default")
            for p in self.pages
        ]

        file_info = DsFileInfoObject(
            filename=self.input.file.name,
            document_hash=self.input.document_hash,
            num_pages=self.input.page_count,
            page_hashes=page_hashes,
        )

        main_text = []
        tables = []
        figures = []

        page_no_to_page = {p.page_no: p for p in self.pages}

        for element in self.assembled.elements:
            # Convert bboxes to lower-left origin.
            target_bbox = DsBoundingBox(
                element.cluster.bbox.to_bottom_left_origin(
                    page_no_to_page[element.page_no].size.height
                ).as_tuple()
            )

            if isinstance(element, TextElement):
                main_text.append(
                    BaseText(
                        text=element.text,
                        obj_type=layout_label_to_ds_type.get(element.label),
                        name=element.label,
                        prov=[
                            Prov(
                                bbox=target_bbox,
                                page=element.page_no,
                                span=[0, len(element.text)],
                            )
                        ],
                    )
                )
            elif isinstance(element, TableElement):
                index = len(tables)
                ref_str = f"#/tables/{index}"
                main_text.append(
                    Ref(
                        name=element.label,
                        obj_type=layout_label_to_ds_type.get(element.label),
                        ref=ref_str,
                    ),
                )

                # Initialise empty table data grid (only empty cells)
                table_data = [
                    [
                        TableCell(
                            text="",
                            # bbox=[0,0,0,0],
                            spans=[[i, j]],
                            obj_type="body",
                        )
                        for j in range(element.num_cols)
                    ]
                    for i in range(element.num_rows)
                ]

                # Overwrite cells in table data for which there is actual cell content.
                for cell in element.table_cells:
                    for i in range(
                        min(cell.start_row_offset_idx, element.num_rows),
                        min(cell.end_row_offset_idx, element.num_rows),
                    ):
                        for j in range(
                            min(cell.start_col_offset_idx, element.num_cols),
                            min(cell.end_col_offset_idx, element.num_cols),
                        ):
                            celltype = "body"
                            if cell.column_header:
                                celltype = "col_header"
                            elif cell.row_header:
                                celltype = "row_header"

                            def make_spans(cell):
                                for rspan in range(
                                    min(cell.start_row_offset_idx, element.num_rows),
                                    min(cell.end_row_offset_idx, element.num_rows),
                                ):
                                    for cspan in range(
                                        min(
                                            cell.start_col_offset_idx, element.num_cols
                                        ),
                                        min(cell.end_col_offset_idx, element.num_cols),
                                    ):
                                        yield [rspan, cspan]

                            spans = list(make_spans(cell))
                            table_data[i][j] = TableCell(
                                text=cell.text,
                                bbox=cell.bbox.to_bottom_left_origin(
                                    page_no_to_page[element.page_no].size.height
                                ).as_tuple(),
                                # col=j,
                                # row=i,
                                spans=spans,
                                obj_type=celltype,
                                # col_span=[cell.start_col_offset_idx, cell.end_col_offset_idx],
                                # row_span=[cell.start_row_offset_idx, cell.end_row_offset_idx]
                            )

                tables.append(
                    DsSchemaTable(
                        num_cols=element.num_cols,
                        num_rows=element.num_rows,
                        obj_type=layout_label_to_ds_type.get(element.label),
                        data=table_data,
                        prov=[
                            Prov(
                                bbox=target_bbox,
                                page=element.page_no,
                                span=[0, 0],
                            )
                        ],
                    )
                )

            elif isinstance(element, FigureElement):
                index = len(figures)
                ref_str = f"#/figures/{index}"
                main_text.append(
                    Ref(
                        name=element.label,
                        obj_type=layout_label_to_ds_type.get(element.label),
                        ref=ref_str,
                    ),
                )
                figures.append(
                    BaseCell(
                        prov=[
                            Prov(
                                bbox=target_bbox,
                                page=element.page_no,
                                span=[0, 0],
                            )
                        ],
                        obj_type=layout_label_to_ds_type.get(element.label),
                        # data=[[]],
                    )
                )

        page_dimensions = [
            PageDimensions(page=p.page_no, height=p.size.height, width=p.size.width)
            for p in self.pages
        ]

        ds_doc = DsDocument(
            name=title,
            description=desc,
            file_info=file_info,
            main_text=main_text,
            tables=tables,
            figures=figures,
            page_dimensions=page_dimensions,
        )

        return ds_doc

    def render_as_dict(self):
        if self.output:
            return self.output.model_dump(by_alias=True, exclude_none=True)
        else:
            return {}

    def render_as_markdown(self):
        if self.output:
            return self.output.export_to_markdown()
        else:
            return ""


class DocumentConversionInput(BaseModel):

    _path_or_stream_iterator: Iterable[Union[Path, DocumentStream]] = None
    limits: Optional[DocumentLimits] = DocumentLimits()

    DEFAULT_BACKEND: ClassVar = PyPdfiumDocumentBackend

    def docs(
        self, pdf_backend: Optional[Type[PdfDocumentBackend]] = None
    ) -> Iterable[InputDocument]:

        pdf_backend = pdf_backend or DocumentConversionInput.DEFAULT_BACKEND

        for obj in self._path_or_stream_iterator:
            if isinstance(obj, Path):
                yield InputDocument(
                    path_or_stream=obj, limits=self.limits, pdf_backend=pdf_backend
                )
            elif isinstance(obj, DocumentStream):
                yield InputDocument(
                    path_or_stream=obj.stream,
                    filename=obj.filename,
                    limits=self.limits,
                    pdf_backend=pdf_backend,
                )

    @classmethod
    def from_paths(cls, paths: Iterable[Path], limits: Optional[DocumentLimits] = None):
        paths = [Path(p) for p in paths]

        doc_input = cls(limits=limits)
        doc_input._path_or_stream_iterator = paths

        return doc_input

    @classmethod
    def from_streams(
        cls, streams: Iterable[DocumentStream], limits: Optional[DocumentLimits] = None
    ):
        doc_input = cls(limits=limits)
        doc_input._path_or_stream_iterator = streams

        return doc_input
