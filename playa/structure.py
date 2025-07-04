"""
Lazy interface to PDF logical structure (PDF 1.7 sect 14.7).
"""

import functools
import logging
import re
from dataclasses import dataclass
from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Pattern,
    Union,
)

from playa.data_structures import NumberTree
from playa.parser import LIT, PDFObject, PSLiteral
from playa.pdftypes import (
    BBOX_NONE,
    ContentStream,
    ObjRef,
    Rect,
    dict_value,
    literal_name,
    resolve1,
    rect_value,
    stream_value,
)
from playa.utils import transform_bbox, get_bound_rects
from playa.worker import (
    DocumentRef,
    PageRef,
    _deref_document,
    _deref_page,
    _ref_document,
)

LOG = logging.getLogger(__name__)
LITERAL_ANNOT = LIT("Annot")
LITERAL_XOBJECT = LIT("XObject")
LITERAL_IMAGE = LIT("Image")
LITERAL_MCR = LIT("MCR")
LITERAL_OBJR = LIT("OBJR")
LITERAL_STRUCTTREEROOT = LIT("StructTreeRoot")
LITERAL_STRUCTELEM = LIT("StructElem")
MatchFunc = Callable[["Element"], bool]

if TYPE_CHECKING:
    from playa.document import Document
    from playa.page import Annotation, Page
    from playa.content import ImageObject, XObjectObject


@dataclass
class ContentItem:
    """Content item in logical structure tree.

    This corresponds to an individual marked content section on a
    specific page, and can be used to (lazily) find that section if
    desired.
    """

    _pageref: PageRef
    mcid: int
    stream: Union[ContentStream, None]
    _bbox: Union[Rect, None] = None

    @property
    def page(self) -> Union["Page", None]:
        """Specific page for this structure tree, if any."""
        if self._pageref is None:
            return None
        return _deref_page(self._pageref)

    @property
    def doc(self) -> "Document":
        """The document containing this content object."""
        docref, _ = self._pageref
        return _deref_document(docref)

    @property
    def bbox(self) -> Rect:
        """Find the bounding box, if any, of this item, which is the
        smallest rectangle enclosing all objects in its marked content
        section.

        Note that this is currently quite inefficient as it involves
        interpreting the entire page.

        If the `page` attribute is `None`, then `bbox` will be
        `BBOX_NONE`.

        """
        if self._bbox is not None:
            return self._bbox
        page = self.page
        if page is None:
            self._bbox = BBOX_NONE
        else:
            itor: Union["Page", "XObjectObject"] = page
            if self.stream is not None:
                for obj in page.xobjects:
                    if obj.stream.objid == self.stream.objid:
                        itor = obj
            self._bbox = get_bound_rects(
                obj.bbox for obj in itor if obj.mcid == self.mcid
            )
        return self._bbox

    @property
    def text(self) -> Union[str, None]:
        """Unicode text contained in this structure element."""
        page = self.page
        if page is None:
            return None
        itor: Union["Page", "XObjectObject"] = page
        if self.stream is not None:
            # FIXME: Potentially quite slow, but we hope this never happens
            for obj in page.xobjects:
                if obj.stream.objid == self.stream.objid:
                    itor = obj
            # FIXME: if we don't find it... then what? (probably None, but...)
        texts = itor.mcid_texts.get(self.mcid)
        if texts is None:
            return None
        return "".join(texts)


@dataclass
class ContentObject:
    """Content object in logical structure tree.

    This corresponds to a content item that is an entire PDF (X)Object
    (PDF 1.7 section 14.7.43), and can be used to (lazily) get that

    The standard is very unclear on what this could be aside from an
    `Annotation` or an `XObject` (presumably either a Form XObject or
    an image).  An XObject must be a content stream, so that's clear
    enough... Otherwise, since the `Type` key is not required in an
    annotation dictionary we presume that this is an annotation if
    it's not present.

    Not to be confused with `playa.page.ContentObject`.  While you
    *can* get there from here with the `obj` property, it may not be a
    great idea, because the only way to do that correctly in the case
    of an `XObject` (or image) is to interpret the containing page.

    Sometimes, but not always, you can nonetheless rapidly access the
    `bbox`, so this is also provided as a property here.

    """

    _pageref: PageRef
    props: Union[ContentStream, Dict[str, PDFObject]]

    @property
    def obj(self) -> Union["XObjectObject", "ImageObject", "Annotation", None]:
        """Return an instantiated object, if possible."""
        objtype = self.type
        if objtype is LITERAL_ANNOT:
            from playa.page import Annotation

            return Annotation.from_dict(self.props, self.page)

        if objtype is LITERAL_XOBJECT:
            assert isinstance(self.props, ContentStream)
            subtype = self.props.get("Subtype")
            itor = self.page.images if subtype is LITERAL_IMAGE else self.page.xobjects
            for obj in itor:
                if obj.stream.objid == self.props.objid:
                    return obj

        return None

    @property
    def type(self) -> PSLiteral:
        """Type of this object, usually LITERAL_ANNOT or LITERAL_XOBJECT."""
        if isinstance(self.props, ContentStream):
            return LITERAL_XOBJECT
        objtype = self.props.get("Type")
        if not isinstance(objtype, PSLiteral):
            return LITERAL_ANNOT
        return objtype

    @property
    def page(self) -> "Page":
        """Containing page for this content object."""
        return _deref_page(self._pageref)

    @property
    def doc(self) -> "Document":
        """The document containing this content object."""
        docref, _ = self._pageref
        return _deref_document(docref)

    @property
    def bbox(self) -> Rect:
        """Find the bounding box, if any, of this object.

        If there is no bounding box (very unlikely) this will be
        `BBOX_NONE`.
        """
        if "BBox" in self.props:
            rawbox = rect_value(self.props["BBox"])
            return transform_bbox(self.page.ctm, rawbox)

        if "Rect" in self.props:
            rawbox = rect_value(self.props["Rect"])
            return transform_bbox(self.page.ctm, rawbox)

        obj = self.obj
        if obj is None:
            return BBOX_NONE
        return obj.bbox


def _find_all(
    elements: List["Element"],
    matcher: Union[str, Pattern[str], MatchFunc, None] = None,
) -> Iterator["Element"]:
    """
    Common code for `find_all()` in trees and elements.
    """

    def match_all(x: "Element") -> bool:
        return True

    def match_tag(x: "Element") -> bool:
        """Match an element name."""
        return x.role == matcher

    def match_regex(x: "Element") -> bool:
        """Match an element name by regular expression."""
        return matcher.match(x.role)  # type: ignore

    if matcher is None:
        match_func = match_all
    elif isinstance(matcher, str):
        match_func = match_tag
    elif isinstance(matcher, re.Pattern):
        match_func = match_regex
    else:
        match_func = matcher  # type: ignore
    elements.reverse()
    while elements:
        el = elements.pop()
        if match_func(el):
            yield el
        for child in reversed(list(el)):
            if isinstance(child, Element):
                elements.append(child)


class Findable(Iterable):
    """find() and find_all() methods that can be inherited to avoid
    repeating oneself"""

    def find_all(
        self, matcher: Union[str, Pattern[str], MatchFunc, None] = None
    ) -> Iterator["Element"]:
        """Iterate depth-first over matching elements in subtree.

        The `matcher` argument is either a string, a regular
        expression, or a function taking a `Element` and returning
        `True` if the element matches, or `None` (default) to return
        all descendants in depth-first order.

        For compatibility with `pdfplumber` and consistent behaviour
        across documents, names and regular expressions are matched
        against the `role` attribute.  If you wish to match the "raw"
        structure type from the `type` attribute, you can do this with
        a matching function.

        """
        return _find_all(list(self), matcher)

    def find(
        self, matcher: Union[str, Pattern[str], MatchFunc, None] = None
    ) -> Union["Element", None]:
        """Find the first matching element in subtree.

        The `matcher` argument is either a string or a regular
        expression to be matched against the `role` attribute, or a
        function taking a `Element` and returning `True` if the
        element matches, or `None` (default) to just get the first
        child element.

        """
        try:
            return next(_find_all(list(self), matcher))
        except StopIteration:
            return None


@dataclass
class Element(Findable):
    """Logical structure element.

    Attributes:
      props: Structure element dictionary (PDF 1.7 table 323).
    """

    _docref: DocumentRef
    props: Dict[str, PDFObject]
    _role: Union[str, None] = None

    @classmethod
    def from_dict(cls, doc: "Document", obj: Dict[str, PDFObject]) -> "Element":
        """Construct from PDF structure element dictionary."""
        return cls(_docref=_ref_document(doc), props=obj)

    @property
    def type(self) -> str:
        """Structure type for this element.

        Note: Raw and standard structure types
            This type is quite likely idiosyncratic and defined by
            whatever style sheets the author used in their word
            processor.  Standard structure types (PDF 1.7 section
            14.8.4) are accessible through the `role_map` attribute of
            the structure root, or, for convenience (this is slow) via
            the `role` attribute on elements.

        """
        return literal_name(self.props["S"])

    @property
    def role(self) -> str:
        """Standardized structure type.

        Note: Roles are always mapped
            Since it is common for documents to use standard types
            directly for some of their structure elements (typically
            ones with no content) and thus to omit them from the role
            map, `role` will always return a string in order to
            facilitate processing.  If you must absolutely know
            whether an element's type has no entry in the role map
            then you will need to consult it directly.
        """
        if self._role is not None:
            return self._role
        tree = self.doc.structure
        if tree is None:  # it could happen!
            return self.type
        return tree.role_map.get(self.type, self.type)

    @property
    def doc(self) -> "Document":
        """Containing document for this element."""
        return _deref_document(self._docref)

    @property
    def page(self) -> Union["Page", None]:
        """Containing page for this element, if any."""
        pg = self.props.get("Pg")
        if pg is None:
            return None
        elif isinstance(pg, ObjRef):
            try:
                return self.doc.pages.by_id(pg.objid)
            except KeyError:
                LOG.warning("'Pg' entry not found in document: %r", self.props)
        else:
            LOG.warning(
                "'Pg' entry is not an indirect object reference: %r", self.props
            )
        return None

    @property
    def parent(self) -> Union["Element", "Tree", None]:
        p = resolve1(self.props.get("P"))
        if p is None:
            return None
        p = dict_value(p)
        if p.get("Type") is LITERAL_STRUCTTREEROOT:
            return self.doc.structure
        return Element.from_dict(self.doc, p)

    @property
    def contents(self) -> Iterator[Union[ContentItem, ContentObject]]:
        """Iterate over all content items contained in an element."""
        for kid in self:
            if isinstance(kid, Element):
                yield from kid.contents
            elif isinstance(kid, (ContentItem, ContentObject)):
                yield kid

    @property
    def bbox(self) -> Rect:
        """The bounding box, if any, of this element.

        Elements may explicitly define a `BBox` in default user space,
        in which case this is used.  Otherwise, the bounding box is
        the smallest rectangle enclosing all of the content items
        contained by this element (which may take some time to compute).

        Note that this is currently quite inefficient as it involves
        interpreting the entire page.

        Note: Elements may span multiple pages!
            In the case of an element (such as a `Document` for
            instance) that spans multiple pages, the bounding box
            cannot exist, and `BBOX_NONE` will be returned.  If the
            `page` attribute is `None`, then `bbox` will be
            `BBOX_NONE`.

        """
        page = self.page
        if page is None:
            return BBOX_NONE
        if "BBox" in self.props:
            rawbox = rect_value(self.props["BBox"])
            return transform_bbox(page.ctm, rawbox)
        else:
            # NOTE: This is quite slow
            return get_bound_rects(
                item.bbox
                for item in self.contents
                if item.page is page and item.bbox is not BBOX_NONE
            )

    def __iter__(self) -> Iterator[Union["Element", ContentItem, ContentObject]]:
        if "K" in self.props:
            kids = resolve1(self.props["K"])
            yield from _make_kids(kids, self.page, self._docref)


@functools.singledispatch
def _make_kids(
    k: PDFObject, page: Union["Page", None], docref: DocumentRef
) -> Iterator[Union["Element", ContentItem, ContentObject]]:
    """
    Make a child for this element from its K array.

    K in Element can be (PDF 1.7 Table 323):
    - a structure element (not a content item)
    - an integer marked-content ID
    - a marked-content reference dictionary
    - an object reference dictionary
    - an array of one or more of the above
    """
    LOG.warning("Unrecognized 'K' element: %r", k)
    yield from ()


@_make_kids.register(list)
def _make_kids_list(
    k: list, page: Union["Page", None], docref: DocumentRef
) -> Iterator[Union["Element", ContentItem, ContentObject]]:
    for el in k:
        yield from _make_kids(resolve1(el), page, docref)


@_make_kids.register(int)
def _make_kids_int(
    k: int, page: Union["Page", None], docref: DocumentRef
) -> Iterator[ContentItem]:
    if page is None:
        LOG.warning("No page found for marked-content reference: %r", k)
        return
    yield ContentItem(_pageref=page.pageref, mcid=k, stream=None)


@_make_kids.register(dict)
def _make_kids_dict(
    k: Dict[str, PDFObject], page: Union["Page", None], docref: DocumentRef
) -> Iterator[Union[ContentItem, ContentObject, "Element"]]:
    ktype = k.get("Type")
    if ktype is LITERAL_MCR:
        yield from _make_kids_mcr(k, page, docref)
    elif ktype is LITERAL_OBJR:
        yield from _make_kids_objr(k, page, docref)
    else:
        yield Element(_docref=docref, props=k)


def _make_kids_mcr(
    k: Dict[str, PDFObject], page: Union["Page", None], docref: DocumentRef
) -> Iterator[ContentItem]:
    mcid = resolve1(k.get("MCID"))
    if mcid is None or not isinstance(mcid, int):
        LOG.warning("'MCID' entry is not an int: %r", k)
        return
    stream: Union[ContentStream, None] = None
    pageref = _get_kid_pageref(k, page, docref)
    if pageref is None:
        return
    try:
        stream = stream_value(k["Stm"])
    except KeyError:
        pass
    except TypeError:
        LOG.warning("'Stm' entry is not a content stream: %r", k)
    # Do not care about StmOwn, we don't do appearances
    yield ContentItem(_pageref=pageref, mcid=mcid, stream=stream)


def _make_kids_objr(
    k: Dict[str, PDFObject], page: Union["Page", None], docref: DocumentRef
) -> Iterator[Union[ContentObject, "Element"]]:
    ref = k.get("Obj")
    if not isinstance(ref, ObjRef):
        LOG.warning("'Obj' entry is not an indirect object reference: %r", k)
        return
    obj: Union[Dict[str, PDFObject], ContentStream] = ref.resolve()
    if not isinstance(obj, (dict, ContentStream)):
        LOG.warning("'Obj' entry does not point to a dict or ContentStream: %r", obj)
        return
    # In theory OBJR is not for elements, but just in case...
    ktype = obj.get("Type")
    if ktype is LITERAL_STRUCTELEM:
        if not isinstance(obj, dict):
            LOG.warning("'Obj' entry does not point to a dict: %r", obj)
            return
        yield Element(_docref=docref, props=obj)
    else:
        pageref = _get_kid_pageref(k, page, docref)
        if pageref is None:
            return
        yield ContentObject(_pageref=pageref, props=obj)


def _get_kid_pageref(
    k: Dict[str, PDFObject], page: Union["Page", None], docref: DocumentRef
) -> Union[PageRef, None]:
    pg = k.get("Pg")
    if pg is not None:
        if isinstance(pg, ObjRef):
            try:
                doc = _deref_document(docref)
                page = doc.pages.by_id(pg.objid)
            except KeyError:
                LOG.warning("'Pg' entry not found in document: %r", k)
        else:
            LOG.warning("'Pg' entry is not an indirect object reference: %r", k)
    if page is None:
        if page is None:
            LOG.warning("No page found for marked-content reference: %r", k)
            return None
    return page.pageref


def _iter_structure(
    doc: "Document",
) -> Iterator[Union["Element", ContentItem, ContentObject]]:
    root = resolve1(doc.catalog.get("StructTreeRoot"))
    if root is None:
        return
    root = dict_value(root)
    kids = resolve1(root.get("K"))
    if kids is None:
        LOG.warning("'K' entry in StructTreeRoot could not be resolved: %r", root)
        return
    # K in StructTreeRoot is special, it can only ever be:
    # - a single element
    # - a list of elements
    if isinstance(kids, dict):
        kids = [kids]
    elif isinstance(kids, list):
        pass
    else:
        LOG.warning(
            "'K' entry in StructTreeRoot should be dict or list but is %r", root
        )
        return
    for k in kids:
        k = resolve1(k)
        # Notwithstanding, we will accept other things in
        # StructTreeRoot, even though, unlike other things forbidden
        # by the PDF standard, it seems that this actually never
        # happens (amazing!).  But we will complain about it.
        if not isinstance(k, dict):
            LOG.warning("'K' entry in StructTreeRoot contains non-element %r", k)
        elif k.get("Type") is LITERAL_OBJR:
            LOG.warning("'K' entry in StructTreeRoot contains object reference %r", k)
        elif k.get("Type") is LITERAL_MCR:
            LOG.warning(
                "'K' entry in StructTreeRoot contains marked content reference %r", k
            )
        yield from _make_kids(k, None, _ref_document(doc))


class Tree(Findable):
    """Logical structure tree.

    A structure tree can be iterated over in the same fashion as its
    elements.  Note that even though it is forbidden for structure
    tree root to contain content items, PLAYA is robust to this
    possibility, thus you should not presume that iterating over it
    will only yield `Element` instances.

    The various attributes (role map, class map, pronunciation
    dictionary, etc, etc) are accessible through `props` but currently
    have no particular interpretation aside from the role map which is
    accessible in normalized form through `role_map`.

    Attributes:
      props: Structure tree root dictionary (PDF 1.7 table 322).
      role_map: Mapping of structure element types (as strings) to
          standard structure types (as strings) (PDF 1.7 section 14.8.4)
      parent_tree: Parent tree linking marked content sections to
          structure elements (PDF 1.7 section 14.7.4.4)
    """

    _docref: DocumentRef
    props: Dict[str, PDFObject]
    _role_map: Dict[str, str]
    _parent_tree: NumberTree

    def __init__(self, doc: "Document") -> None:
        self._docref = _ref_document(doc)
        self.props = dict_value(doc.catalog["StructTreeRoot"])

    def __iter__(self) -> Iterator[Union["Element", ContentItem, ContentObject]]:
        doc = _deref_document(self._docref)
        return _iter_structure(doc)

    @property
    def role_map(self) -> Dict[str, str]:
        """Dictionary mapping some (not necessarily all) element types
        to their standard equivalents."""
        if hasattr(self, "_role_map"):
            return self._role_map
        self._role_map = {}
        rm = resolve1(self.props.get("RoleMap"))  # It is optional
        if isinstance(rm, dict):
            for k, v in rm.items():
                if isinstance(v, PSLiteral):
                    role = literal_name(v)
                else:
                    role = str(v)
                self._role_map[k] = role
        return self._role_map

    @property
    def parent_tree(self) -> NumberTree:
        """Parent tree for this document.

        This is a somewhat obscure data structure that links marked
        content sections to their corresponding structure elements.
        If you don't know what that means, you probably don't need it,
        but if you do, here it is.

        Unlike the structure tree itself, if there is no parent tree,
        this will be an empty NumberTree.  This is because the parent
        tree is required by the spec in the case where structure
        elements contain marked content, which is nearly all the time.

        """
        if hasattr(self, "_parent_tree"):
            return self._parent_tree
        if "ParentTree" not in self.props:
            self._parent_tree = NumberTree({})
        else:
            self._parent_tree = NumberTree(self.props["ParentTree"])
        return self._parent_tree

    @property
    def contents(self) -> Iterator[Union[ContentItem, ContentObject]]:
        """Iterate over all content items in the tree."""
        for kid in self:
            if isinstance(kid, Element):
                yield from kid.contents
            elif isinstance(kid, (ContentItem, ContentObject)):
                # This is not supposed to happen, but we will support it anyway
                yield kid

    @property
    def doc(self) -> "Document":
        """Document with which this structure tree is associated."""
        return _deref_document(self._docref)
