from __future__ import annotations

import json
import uuid
from collections import namedtuple
from copy import deepcopy
from os import stat
from pathlib import Path
from threading import RLock

import arrow

from remedy.remarkable.lines import Layer, readLines
from remedy.remarkable.pdfbase import PDFBase
from remedy.utils import deepupdate, log

FOLDER_TYPE = 'CollectionType'
DOCUMENT_TYPE = 'DocumentType'

NOTEBOOK = 1
PDF = 2
EPUB = 4
FOLDER = 8
UNKNOWN = 16
DELETED_NOTEBOOK = NOTEBOOK << 4
DELETED_PDF = PDF << 4
DELETED_EPUB = EPUB << 4
DELETED_FOLDER = FOLDER << 4

NOTEBOOK = NOTEBOOK | DELETED_NOTEBOOK
PDF = PDF | DELETED_PDF
EPUB = EPUB | DELETED_EPUB
FOLDER = FOLDER | DELETED_FOLDER


class RemarkableError(Exception):
    pass


class RemarkableSourceError(RemarkableError):
    pass


class RemarkableUidCollision(RemarkableError):
    pass


# TODO:
# - MoveTo method of index (special case for trash, take care of deleted field)
# - Consider an `update` method for Entry, triggering a save of json files on tablet


class Entry:
    @staticmethod
    def from_dict(index, uid, metadata, content) -> Entry:
        if 'type' not in metadata:
            return Unknown(index, uid, metadata, content)

        if metadata['type'] == FOLDER_TYPE:
            return Folder(index, uid, metadata, content, type_name='folder')

        if metadata['type'] == DOCUMENT_TYPE:
            if content['fileType'] in ['', 'notebook']:
                return Notebook(index, uid, metadata, content, type_name='notebook')
            if content['fileType'] == 'pdf':
                return PDFBasedDoc(index, uid, metadata, content, type_name='pdf')
            if content['fileType'] == 'epub':
                return PDFBasedDoc(index, uid, metadata, content, type_name='epub')

        return Unknown(index, uid, metadata, content)

    def __init__(
        self, index, uid, metadata=None, content=None, type_name='unknown'
    ) -> None:
        self.type_name = type_name
        self.index = index
        self.uid = uid
        self._metadata = metadata if metadata is not None else {}
        self._content = content if content is not None else {}

        self._metadata.setdefault('parent', ROOT_ID)
        self._metadata.setdefault('deleted', False)
        self._metadata.setdefault('visibleName', uid)

        self._postInit()

    def _postInit(self):
        pass

    def isRoot(self):
        return self.uid == ROOT_ID and self.parent is None

    def name(self):
        return self._metadata.get('visibleName')

    def isDeleted(self):
        return self.index.isDeleted(self.uid)

    def isIndirectlyDeleted(self):
        return self.index.isIndirectlyDeleted(self.uid)

    def isFolder(self):
        return self.index.isFolder(self.uid)

    def parentEntry(self):
        if self.parent is None:
            return None
        return self.index.get(self.parent)

    def ancestry(self):
        return self.index.ancestryOf(self.uid, exact=True)

    def path(self, delim='/'):
        return self.index.pathOf(self.uid, delim=delim)

    def fullPath(self, includeSelf=False):
        return self.index.fullPathOf(self.uid, includeSelf=includeSelf)

    def updatedOn(self, default='Unknown'):
        try:
            return self.updatedDate().humanize()
        except Exception as e:
            return default

    _updatedDate = None

    def updatedDate(self):
        if self._updatedDate is None:
            try:
                self._updatedDate = arrow.get(int(self.lastModified) / 1000)
            except Exception:
                # self._updatedDate = arrow.utcnow()
                pass
        return self._updatedDate

    def updatedFullDate(self, default='Unknown'):
        try:
            return self.updatedDate().format('d MMM YYYY [at] hh:mm')
        except Exception as e:
            return default

    def openedFullDate(self, default='Unknown'):
        try:
            return arrow.get(int(self.lastOpened) / 1000).format(
                'd MMM YYYY [at] hh:mm'
            )
        except Exception as e:
            return default

    def size(self):
        if self.sizeInBytes is None:
            return None
        num = int(self.sizeInBytes)
        # https://stackoverflow.com/a/1094933/2753846
        suffix = 'B'
        for unit in ['', 'K', 'M', 'G', 'T', 'P', 'E', 'Z']:
            if abs(num) < 1024.0:
                return f'{num:3.1f} {unit}{suffix}'
            num /= 1024.0
        return f'{num:.1f} Y{suffix}'

    def cover(self):
        return self.get('coverPageNumber', self.get('lastOpenedPage', 0))

    def allDocTags(self):
        return {t['name'] for t in self.tags or []}

    def allPageTags(self):
        return {t['name'] for t in self.pageTags or []}

    def allTags(self):
        return self.allDocTags() | self.allPageTags()

    def get(self, field, default=None):
        if field in self._metadata:
            return self._metadata[field]

        if field in self._content:
            return self._content[field]

        return default

    def __getattr__(self, field):
        if field == 'fsource' and self.index:
            return self.index.fsource
        if field in self._metadata:
            return self._metadata[field]
        if field in self._content:
            return self._content[field]
        return None

    def __dir__(self):
        return (
            ['name', 'updatedOn', 'isDeleted', 'get', 'fsource']
            + list(self._metadata.keys())
            + list(self._content.keys())
        )


class Folder(Entry):
    def _postInit(self):
        self.files = []
        self.folders = []

    def items(self):
        yield from self.folders
        yield from self.files


class Unknown(Entry):
    ...


ROOT_ID = ''
TRASH_ID = 'trash'


class Document(Entry):
    def getPageId(self, pageNum):
        if self.pages is None:
            return str(pageNum)
        else:
            return self.pages[pageNum]

    def getPage(self, pageNum) -> Page:
        try:
            pid = self.getPageId(pageNum)
            rmfile = self.fsource.retrieve(self.uid, pid, ext='rm')
            with open(rmfile, 'rb') as f:
                (ver, layers) = readLines(f)
        except:
            ver = 5
            layers = []
        else:
            try:
                mfile = self.fsource.retrieve(self.uid, pid + '-metadata', ext='json')
                with open(mfile) as f:
                    layerNames = json.load(f)
                layerNames = layerNames['layers']
            except Exception:
                layerNames = [{'name': 'Layer %d' % j} for j in range(len(layers))]

            highlights = {}
            try:
                if self.fsource.exists(self.uid + '.highlights', pid, ext='json'):
                    hfile = self.fsource.retrieve(
                        self.uid + '.highlights', pid, ext='json'
                    )
                    with open(hfile) as f:
                        h = json.load(f).get('highlights', [])
                    for i in range(len(h)):
                        highlights[i] = h[i]
            except Exception:
                pass  # empty highlights are ok

            for j in range(len(layers)):
                layers[j] = Layer(
                    layers[j], layerNames[j].get('name'), highlights.get(j, [])
                )

        return self._makePage(layers, ver, pageNum)

    def num_pages(self) -> int:
        if self.pageCount is not None:
            return self.pageCount

        if self.pages is not None:
            return len(self.pages)

        return 0

    def numHighlightedPages(self) -> int:
        return sum(
            1 for _ in self.fsource.listSubItems(self.uid + '.highlights', ext='json')
        )

    def numMarkedPages(self) -> int:
        return sum(1 for _ in self.fsource.listSubItems(self.uid, ext='rm'))

    def highlights(self):
        highlights = []
        pageCount = self.num_pages()
        pages = self.pages
        if pages is None:
            pages = range(0, pageCount)
        else:
            pageCount = max(pageCount, len(pages))

        pageRange = range(0, pageCount)
        for i in pageRange:
            pid = pages[i]
            if self.fsource.exists(self.uid + '.highlights', pid, ext='json'):
                hfile = self.fsource.retrieve(self.uid + '.highlights', pid, ext='json')
                try:
                    with open(hfile) as f:
                        h = json.load(f)
                except Exception:
                    pass
                else:
                    h['pageNum'] = i + 1
                    h['pageId'] = pid
                    highlights.append(h)

        return highlights

    def marked(self, pageNum) -> bool:
        pid = self.getPageId(pageNum)
        return self.fsource.exists(self.uid, pid, ext='rm') or self.fsource.exists(
            self.uid + '.highlights', pid, ext='json'
        )

    def _makePage(self, layers, version, pageNum) -> Page:
        return Page(layers, version, pageNum, document=self)

    def retrieveBaseDocument(self):
        return None

    def shouldHaveBaseDocument(self):
        return False

    def hasBaseDocument(self):
        return False

    def baseDocument(self):
        return None

    def baseDocumentName(self):
        return None

    def canRenderBase(self):
        base = self.baseDocument()
        return (base is None) or base.canRender()


class Page:
    def __init__(
        self, layers, version, pageNum=None, document=None, background=None
    ) -> None:
        self.layers = layers
        self.version = version
        self.pageNum = pageNum
        self.document = document
        self.background = background


Template = namedtuple('Template', ['name', 'path'])

# Here 'background' is either None or a Template object.
# Subclasses of Page may use additional types.
# For annotated pdfs, the underlying PDF page needs to be fetched separately
# and the 'background' field will be None by default.


class Notebook(Document):
    def _postInit(self):
        try:
            pfile = self.fsource.retrieve(self.uid, ext='pagedata')
            with open(pfile, encoding='utf-8') as f:
                self._bg = [t.rstrip('\n') for t in f.readlines()]
        except OSError:
            pass

    def _makePage(self, layers, version, pageNum) -> Page:
        t = self._bg[pageNum] if pageNum < len(self._bg) else None
        if t:
            template = Template(t, path=(lambda: self.fsource.retrieveTemplate(t)))
        else:
            template = None

        return Page(layers, version, pageNum, document=self, background=template)


class PDFBasedDoc(Document):
    def _postInit(self):
        self._pdf = PDFBase(self)

    def _makePage(self, layers, version, pageNum) -> Page:
        return Page(layers, version, pageNum, document=self)

    def markedPages(self):
        for i, p in enumerate(self.pages):
            if self.fsource.exists(self.uid, p, ext='rm'):
                yield i

    def retrieveBaseDocument(self):
        b = self.baseDocumentName()
        if b and self.fsource.exists(b):
            return self.fsource.retrieve(b)
        return None

    def shouldHaveBaseDocument(self):
        return True

    def hasBaseDocument(self):
        b = self.baseDocumentName()
        return b and self.fsource.exists(b)

    def baseDocument(self):
        return self._pdf

    def baseDocumentName(self):
        return self.uid + '.pdf'

    def num_pages(self) -> int:
        result = super().num_pages()
        if result == 0:
            return self._pdf.pageCount()

        return result


DOC_BASE_METADATA = {
    'deleted': False,
    'metadatamodified': True,
    'modified': True,
    'parent': '',
    'pinned': False,
    'synced': False,
    'type': 'DocumentType',
    'version': 0,
}
# "lastModified": "1592831071604",
# "visibleName": ""


PDF_BASE_CONTENT = {
    'dummyDocument': False,
    'extraMetadata': {},
    'fileType': 'pdf',
    'fontName': '',
    'lastOpenedPage': 0,
    'legacyEpub': False,
    'lineHeight': -1,
    'margins': 100,
    'orientation': 'portrait',
    'pageCount': 0,
    'textAlignment': 'left',
    'textScale': 1,
    'transform': {
        'm11': 1,
        'm12': 0,
        'm13': 0,
        'm21': 0,
        'm22': 1,
        'm23': 0,
        'm31': 0,
        'm32': 0,
        'm33': 1,
    },
}

EPUB_BASE_CONTENT = {
    'dummyDocument': False,
    'extraMetadata': {},
    'fileType': 'epub',
    'fontName': 'Noto Serif',
    'legacyEpub': False,
    'lineHeight': 150,
    'margins': 200,
    'orientation': 'portrait',
    'textAlignment': 'justify',
    'textScale': 0.8,
    'lastOpenedPage': 0,
    'pageCount': 0,
    'transform': {
        'm11': 1,
        'm12': 0,
        'm13': 0,
        'm21': 0,
        'm22': 1,
        'm23': 0,
        'm31': 0,
        'm32': 0,
        'm33': 1,
    },
}

DOC_BASE_CONTENT = {
    PDF: PDF_BASE_CONTENT,
    EPUB: EPUB_BASE_CONTENT,
}


FOLDER_METADATA = {
    'deleted': False,
    'metadatamodified': True,
    'modified': True,
    'parent': '',
    'pinned': False,
    'synced': False,
    'type': 'CollectionType',
    'version': 0,
    # "lastModified": "timestamp",
    # "visibleName": "..."
}


class RemarkableIndex:
    _upd_lock = RLock()

    def __init__(self, fsource, progress=(lambda x, tot: None)):
        self.fsource = fsource
        uids = list(fsource.listItems())
        self.root = Folder(
            self,
            ROOT_ID,
            metadata={
                'visibleName': 'reMarkable',
                'parent': None,
                'deleted': False,
                'type': FOLDER_TYPE,
            },
            content={},
            type_name='folder',
        )
        self.trash = Folder(
            self,
            TRASH_ID,
            metadata={
                'visibleName': 'Trash',
                'parent': ROOT_ID,
                'deleted': False,
                'type': FOLDER_TYPE,
            },
            content={},
            type_name='trash',
        )
        self.index = {ROOT_ID: self.root, TRASH_ID: self.trash}
        self.tags = {}

        j = 0
        for j, uid in enumerate(uids):
            progress(j, len(uids))

            metadata = self.fsource.readJson(uid, ext='metadata')
            content = self.fsource.readJson(uid, ext='content')

            for t in content.get('tags', []):
                if t['name'] not in self.tags:
                    self.tags[t['name']] = {
                        'docs': [],
                        'pages': [],
                    }
                self.tags[t['name']]['docs'].append(uid)

            for t in content.get('pageTags', []):
                if t['name'] not in self.tags:
                    self.tags[t['name']] = {
                        'docs': [],
                        'pages': [],
                    }
                self.tags[t['name']]['pages'].append({'doc': uid, 'page': t['pageId']})

            self.index[uid] = Entry.from_dict(self, uid, metadata, content)

        # create folders hierarchy
        for uid, entry in self.index.items():
            parent = TRASH_ID if entry.deleted else entry.parent
            if parent is None:
                continue

            if entry.type == FOLDER_TYPE:
                self.index[parent].folders.append(uid)
            else:
                self.index[parent].files.append(uid)

    def _new_entry_prepare(self, uid, etype, meta, path=None):
        pass  # for subclasses to specialise

    def _new_entry_progress(self, uid, done, tot):
        pass  # for subclasses to specialise

    def _new_entry_error(self, exception, uid, etype, meta, path=None):
        pass  # for subclasses to specialise

    def _new_entry_complete(self, uid, etype, meta, path=None):
        pass  # for subclasses to specialise

    def _update_entry_prepare(self, uid, new_meta, new_content):
        pass  # for subclasses to specialise

    def _update_entry_complete(self, uid, new_meta, new_content):
        pass  # for subclasses to specialise

    def _update_entry_error(self, exception, uid, new_meta, new_content):
        pass  # for subclasses to specialise

    def isReadOnly(self):
        return self.fsource.isReadOnly()

    def get(self, uid):
        if uid in self.index:
            return self.index[uid]

        raise RemarkableError('Uid %s not found!' % uid)

    def allUids(self):
        return self.index.keys()

    def ancestryOf(self, uid, exact=True, includeSelf=False, reverse=True):
        if not exact:
            uid = self.matchId(uid)

        p = []
        while uid != TRASH_ID and uid != ROOT_ID:
            if uid not in self.index:
                # hierarchy is broken: this is a dangling entry
                return None

            p.append(uid)

            entry = self.get(uid)
            uid = TRASH_ID if entry.deleted else entry.parent

        if not includeSelf:
            p = p[1:]
        if reverse:
            p = reversed(p)
        return p

    def pathOf(
        self, uid, exact: bool = True, includeSelf: bool = False, delim: str = '/'
    ) -> str:
        return delim.join(
            self.nameOf(x) for x in self.ancestryOf(uid, exact, includeSelf)
        )

    def fullPathOf(self, uid, includeSelf: bool = False):
        p = self.pathOf(uid, includeSelf=includeSelf)
        if (not includeSelf) or self.isFolder(uid):
            p += '/'
        if not p.startswith('/'):
            p = '/' + p
        return p

    def matchId(self, pid):
        for k in self.index:
            if k.startswith(pid):
                return k
        return None

    def isFolder(self, uid):
        return uid in self.index and self.index[uid].type_name in ('folder', 'trash')

    def updatedOn(self, uid):
        try:
            updated = arrow.get(int(self.lastModifiedOf(uid)) / 1000).humanize()
        except Exception as e:
            updated = self.lastModifiedOf(uid) or 'Unknown'
        return updated

    def nameOf(self, uid):
        return self.get(uid).visibleName

    def isDeleted(self, uid):
        return uid in self.index and (
            self.index[uid].deleted or self.index[uid].parent == TRASH_ID
        )

    def isIndirectlyDeleted(self, uid):
        return any(
            self.isDeleted(a)
            for a in self.ancestryOf(uid, includeSelf=True, reverse=False)
        )

    def __getattr__(self, field):
        if field.endswith('Of'):
            return (
                lambda uid: self.index[uid].get(field[:-2])
                if uid in self.index
                else None
            )
        else:
            raise AttributeError(field)

    def scanFolders(self, uid=ROOT_ID):
        if isinstance(uid, Entry):
            n = uid
        else:
            n = self.index[uid]

        if isinstance(n, Folder):
            stack = [n]  # stack of folders
            while stack:
                n = stack.pop()
                yield n
                for f in n.folders:
                    stack.append(self.index[f])

    _reservedUids = set()

    def reserveUid(self):
        # collisions are highly unlikely, but good to check
        uid = str(uuid.uuid4())
        while uid in self.index or uid in self._reservedUids:
            uid = str(uuid.uuid4())
        self._reservedUids.add(uid)
        return uid

    def newFolder(self, uid=None, progress=None, **metadata):
        try:
            if self.isReadOnly():
                raise RemarkableSourceError(
                    "The file source '%s' is read-only" % self.fsource.name
                )

            if not uid:
                uid = self.reserveUid()

            log.info('Preparing creation of %s', uid)
            self._new_entry_prepare(uid, FOLDER, metadata)

            def p(x):
                if callable(progress):
                    progress(x, 2)
                self._new_entry_progress(uid, x, 2)

            if self.fsource.exists(uid, ext='metadata'):
                raise RemarkableUidCollision(
                    'Attempting to create new document but chosen uuid is in use'
                )

            p(0)

            meta = FOLDER_METADATA.copy()
            meta.setdefault('visibleName', 'New Folder')
            meta.setdefault('lastModified', str(arrow.utcnow().int_timestamp * 1000))
            meta.update(metadata)
            if not self.isFolder(meta['parent']):
                raise RemarkableError('Cannot find parent %s' % meta['parent'])

            self.fsource.store(meta, uid + '.metadata')
            p(1)
            self.fsource.store({}, uid + '.content')
            p(2)

            self.index[uid] = d = Folder(self, uid, meta, {}, type_name='folder')
            self.index[d.parent].folders.append(uid)
            self._reservedUids.discard(uid)

            self._new_entry_complete(uid, FOLDER, metadata)
            return uid
        except Exception as e:
            # cleanup if partial upload
            self.fsource.remove(uid + '.metadata')
            self.fsource.remove(uid + '.content')
            self._new_entry_error(e, uid, FOLDER, metadata)
            raise e

    def newDocument(self, path=None, uid=None, content={}, progress=None, **metadata):
        try:
            if self.isReadOnly():
                raise RemarkableSourceError(
                    "The file source '%s' is read-only" % self.fsource.name
                )

            if not uid:
                uid = self.reserveUid()
            path = Path(path)
            ext = path.suffix.lower()
            if ext == '.pdf':
                etype = PDF
            elif ext == '.epub':
                etype = EPUB
            else:
                raise RemarkableError(
                    'Can only upload PDF and EPUB files, but was given a %s' % ext
                )

            log.info('Preparing creation of %s', uid)
            self._new_entry_prepare(uid, etype, metadata, path)

            totBytes = 0
            if callable(progress):

                def p(x):
                    progress(x, totBytes)
                    self._new_entry_progress(uid, x, totBytes)

                def up(x, t):
                    p(400 + x)

            else:

                def p(x, t=0):
                    pass

                up = None

            if self.fsource.exists(uid, ext='metadata'):
                raise RemarkableUidCollision(
                    'Attempting to create new document but chosen uuid is in use'
                )

            meta = DOC_BASE_METADATA.copy()
            meta.setdefault('visibleName', path.stem)
            meta.setdefault('lastModified', str(arrow.utcnow().int_timestamp * 1000))
            deepupdate(meta, metadata)
            if not self.isFolder(meta['parent']):
                raise RemarkableError('Cannot find parent %s' % meta['parent'])

            cont = deepcopy(DOC_BASE_CONTENT[etype])
            deepupdate(cont, content)

            # imaginary 100bytes per json file
            totBytes = 400 + stat(path).st_size

            p(0)
            self.fsource.store(meta, uid + '.metadata')
            p(200)
            self.fsource.store(cont, uid + '.content')
            p(300)
            self.fsource.store('', uid + '.pagedata')
            p(400)
            self.fsource.upload(path, uid + ext, progress=up)
            self.fsource.makeDir(uid)

            if etype == PDF:
                d = PDFBasedDoc(self, uid, meta, cont, type_name='pdf')
            else:
                d = PDFBasedDoc(self, uid, meta, cont, type_name='epub')
            self.index[uid] = d
            self.index[d.parent].files.append(uid)
            self._reservedUids.discard(uid)

            p(totBytes)
            self._new_entry_complete(uid, etype, metadata, path)

            return uid

        except Exception as e:
            # cleanup if partial upload
            self.fsource.remove(uid + ext)
            self.fsource.remove(uid + '.metadata')
            self.fsource.remove(uid + '.content')
            self.fsource.remove(uid + '.pagedata')
            self.fsource.removeDir(uid)
            self._new_entry_error(e, uid, etype, metadata, path)
            raise e

    def update(self, uid, content={}, **metadata):
        # If you need this to look atomic vs concurrent reads
        # of metadata modify only one field at a time
        try:
            with self._upd_lock:
                self._update_entry_prepare(uid, metadata, content)

                if uid == ROOT_ID or uid == TRASH_ID:
                    raise RemarkableError('Cannot update root and trash entries')

                entry = self.get(uid)

                if content:
                    cont = deepcopy(entry._content)
                    deepupdate(cont, content)
                    self.fsource.store(cont, uid + '.content', overwrite=True)
                    entry._content = cont

                if metadata or content:  # if content changed, bump version
                    new_parent = old_parent = None  # flagging no reparenting needed
                    if 'type' in metadata:
                        raise RemarkableError('Cannot change type of document')
                    # Safety checks for move operations
                    if 'parent' in metadata:
                        old_parent = entry.parentEntry()
                        new_parent = self.get(metadata['parent'])
                        if not new_parent.isFolder():
                            raise RemarkableError(
                                'Cannot change parent of %s to %s which is not a folder'
                                % (uid, new_parent.uid)
                            )
                        if entry.isFolder() and uid in new_parent.ancestry():
                            raise RemarkableError(
                                'Circularity would be introduced by making %s a parent of %s'
                                % (new_parent.uid, uid)
                            )
                    meta = deepcopy(entry._metadata)
                    metadata.setdefault(
                        'lastModified', str(arrow.utcnow().int_timestamp * 1000)
                    )
                    metadata.setdefault('metadatamodified', True)
                    metadata.setdefault('version', entry.version + 1)
                    deepupdate(meta, metadata)
                    self.fsource.store(meta, uid + '.metadata', overwrite=True)

                    entry._metadata = meta
                    if new_parent is not None:
                        if entry.isFolder():
                            old_parent.folders.remove(uid)
                            new_parent.folders.append(uid)
                        else:
                            old_parent.files.remove(uid)
                            new_parent.files.append(uid)

                self._update_entry_complete(uid, metadata, content)
        except Exception as e:
            self._update_entry_error(e, uid, metadata, content)
            raise e

    def moveToTrash(self, uid):
        with self._upd_lock:
            if not self.isDeleted(uid):
                self.update(uid, parent=TRASH_ID)

    def rename(self, uid, new_name):
        self.update(uid, visibleName=new_name)

    def newFolderWith(self, uids=[], **metadata):
        with self._upd_lock:
            fuid = self.newFolder(**metadata)
            for uid in uids:
                self.update(uid, parent=fuid)

    def moveAll(self, uids, parent):
        with self._upd_lock:
            for uid in uids:
                self.update(uid, parent=parent)
