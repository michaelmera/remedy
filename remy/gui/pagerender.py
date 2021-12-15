from remy import *

from PyQt5.QtGui import *
from PyQt5.QtWidgets import *
from PyQt5.QtCore import *
from PyQt5.QtPrintSupport import *

import remy.remarkable.constants as rm

from itertools import groupby

import time
import logging
log = logging.getLogger('remy')

try:

  from simplification.cutil import simplify_coords

  def simpl(stroke, tolerance=10.0):
    return simplify_coords([[s.x, s.y] for s in stroke.segments], tolerance)

except Exception:
  simpl = None


def dynamic_width(segment):
  return (segment.width,None)

def semi_dynamic_width(segment):
  return (round(segment.width),None)

def very_dynamic_width(segment):
  return (round((segment.width *.7) + (segment.width * .3 * segment.pressure),2),None)

def pencil_width(segment):
  return (round(segment.width*.55,2),pencilBrushes().getIndex(segment.pressure))

def mech_pencil_width(segment):
  return (round(segment.width/1.5,2),pencilBrushes().getIndex(segment.pressure))

def flat_pencil_width(segment):
  return (round(segment.width*.55,2),round(segment.pressure, 2))

def flat_mech_pencil_width(segment):
  return (round(segment.width/1.5,2),round(segment.pressure, 2))

def const_width(w):
    return lambda segment: (w,None)

def _progress(p, i, t):
  if callable(p):
    p(i, t)


class PencilBrushes():

  def __init__(self, N=15, size=200):
    from random import randint
    self._textures = []
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    for i in range(N):
      for j in range(int(size*size*(i+1)/N/2.5)):
        img.setPixelColor(randint(0,size-1),randint(0,size-1),Qt.black)
      self._textures.append(img.copy())

  def getIndex(self, i):
    i = int(i * (len(self._textures)-1))
    return max(0, min(i, len(self._textures)-1))

  def getTexture(self, i):
    return self._textures[max(0,min(i, len(self._textures)-1))]

  def getBrush(self, i, scale=.4):
    b = QBrush(self.getTexture(i))
    if scale != 1:
      tr = QTransform()
      tr.scale(scale,scale)
      b.setTransform(tr)
    return b

_pencilBrushes = None
def pencilBrushes(**kw):
  global _pencilBrushes
  if _pencilBrushes is None:
    _pencilBrushes = PencilBrushes(**kw)
  return _pencilBrushes


DEFAULT_COLORS = [Qt.black, QColor('#bbbbbb'), Qt.white]
DEFAULT_HIGHLIGHT = QColor(255,235,147, 80)

QUICK_ERASER    = 0
IGNORE_ERASER   = 1
ACCURATE_ERASER = 2
AUTO_ERASER = 4
AUTO_ERASER_IGNORE = 4
AUTO_ERASER_ACCURATE = 5

ERASER_MODE = {
  "quick": QUICK_ERASER,
  "ignore": IGNORE_ERASER,
  "accurate": ACCURATE_ERASER,
  "auto": AUTO_ERASER,
}


def bezierInterpolation(K, coord):
  n = len(K)-1
  p1=[0.]*n
  p2=[0.]*n

  a=[0.]*n
  b=[0.]*n
  c=[0.]*n
  r=[0.]*n

  a[0]=0.
  b[0]=2.
  c[0]=1.
  r[0] = K[0][coord]+2.*K[1][coord]

  for i in range(1,n-1):
    a[i]=1.
    b[i]=4.
    c[i]=1.
    r[i] = 4. * K[i][coord] + 2. * K[i+1][coord]

  a[n-1]=2.
  b[n-1]=7.
  c[n-1]=0.
  r[n-1] = 8. *K[n-1][coord] + K[n][coord]

  # Thomas algorithm
  for i in range(1,n):
    m = a[i]/b[i-1]
    b[i] = b[i] - m * c[i - 1]
    r[i] = r[i] - m * r[i-1]

  p1[n-1] = r[n-1]/b[n-1]
  for i in range(n-2,-1,-1):
    p1[i] = (r[i] - c[i] * p1[i+1]) / b[i]

  for i in range(n-1):
    p2[i]=2.*K[i+1][coord]-p1[i+1]

  p2[n-1]=0.5*(K[n][coord]+p1[n-1])

  return (p1,p2)



class PageGraphicsItem(QGraphicsRectItem):

  def __init__(
      self,
      page,
      colors=DEFAULT_COLORS,
      highlight=DEFAULT_HIGHLIGHT,
      pencil_resolution=.4,
      simplify=0,
      smoothen=False,
      eraser_mode=AUTO_ERASER,
      parent=None,
      progress=None,
      exclude_layers=set()
  ):
    super().__init__(0,0,rm.WIDTH,rm.HEIGHT,parent)

    if isinstance(eraser_mode, str):
      eraser_mode = ERASER_MODE.get(eraser_mode, AUTO_ERASER)
    if isinstance(colors, dict):
      highlight = colors.get('highlight', highlight)
      colors = [
        QColor(colors.get('black', DEFAULT_COLORS[0])),
        QColor(colors.get('gray', DEFAULT_COLORS[1])),
        QColor(colors.get('white', DEFAULT_COLORS[2])),
      ]

    if simpl is None:
      simplify = 0
      log.warning("Simplification parameters ignored since the simplification library is not installed")

    noPen = QPen(Qt.NoPen)
    noPen.setWidth(0)
    self.setPen(noPen)
    eraserStroker = QPainterPathStroker()
    eraserStroker.setCapStyle(Qt.RoundCap)
    eraserStroker.setJoinStyle(Qt.RoundJoin)

    pen = QPen()
    pen.setWidth(1)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)

    totalStrokes = sum(len(l.strokes) for l in page.layers)
    curStroke = 0
    _progress(progress,curStroke,totalStrokes); curStroke += 1

    for li, l in enumerate(page.layers):
      if li+1 in exclude_layers or l.name in exclude_layers:
        continue
      if (l.highlights
          and l.name + "/highlights" not in exclude_layers
          and str(li+1) + "/highlights" not in exclude_layers):
        # then
        h = QGraphicsRectItem(self)
        h.setPen(QPen(Qt.NoPen))
        for hi in l.highlights:
          for r in hi.get('rects', []):
            ri = QGraphicsRectItem(r.get('x',0),r.get('y',0),r.get('width',0), r.get('height',0), h)
            ri.setPen(QPen(Qt.NoPen))
            ri.setBrush(highlight)
            ri.setToolTip(hi.get('text',''))
      group = QGraphicsPathItem()
      group.setPen(noPen)
      if eraser_mode >= AUTO_ERASER:
        eraser_mode = AUTO_ERASER_IGNORE

      for k in l.strokes:
        tool = rm.TOOL_ID.get(k.pen)
        # print(rm.TOOL_NAME.get(tool))

        # COLOR
        if tool == rm.HIGHLIGHTER_TOOL:
          pen.setColor(highlight)
        elif tool == rm.ERASER_TOOL:
          pen.setColor(Qt.white)
        else:
          pen.setColor(colors[k.color])

        # WIDTH CALCULATION
        if tool == rm.PENCIL_TOOL:
          if pencil_resolution:
            calcwidth = pencil_width
          else:
            calcwidth = flat_pencil_width
        elif tool == rm.MECH_PENCIL_TOOL:
          if pencil_resolution:
            calcwidth = mech_pencil_width
          else:
            calcwidth = flat_mech_pencil_width
        elif tool == rm.BALLPOINT_TOOL:
          calcwidth = semi_dynamic_width
          # calcwidth = const_width(k.width)
        else:
          calcwidth = dynamic_width

        # AUTO ERASER SETTINGS
        if tool == rm.BRUSH_TOOL or tool == rm.MARKER_TOOL:
          if eraser_mode == AUTO_ERASER:
            eraser_mode = AUTO_ERASER_ACCURATE
        elif tool == rm.PENCIL_TOOL:
          if eraser_mode == AUTO_ERASER:
            if max(s.width for s in k.segments) > 2:
              eraser_mode = AUTO_ERASER_ACCURATE

        pen.setWidthF(0)
        path = None

        if k.pen == 8:
          # ERASE AREA
          # The remarkable renderer seems to ignore these!
          pass
        elif k.pen == 6 and eraser_mode % 3 == IGNORE_ERASER:
          pass
        elif k.pen == 6 and eraser_mode % 3 == ACCURATE_ERASER:
          # ERASER
          T1 = time.perf_counter()
          eraserStroker.setWidth(k.width)
          area = QPainterPath(QPointF(0,0))
          area.moveTo(0,0)
          area.lineTo(0,rm.HEIGHT)
          area.lineTo(rm.WIDTH,rm.HEIGHT)
          area.lineTo(rm.WIDTH,0)
          area.lineTo(0,0)
          subarea = QPainterPath(QPointF(k.segments[0].x, k.segments[0].y))
          for s in k.segments[1:]:
            subarea.lineTo(s.x,s.y)
          subarea = eraserStroker.createStroke(subarea)
          log.debug('A: %f', time.perf_counter() - T1); T1 = time.perf_counter()
          subarea = subarea.simplified()  # this is expensive
          log.debug('B: %f', time.perf_counter() - T1); T1 = time.perf_counter()
          # area = fullPageClip.subtracted(subarea)  # this alternative is also expensive
          area.addPath(subarea)
          group.setFlag(QGraphicsItem.ItemClipsChildrenToShape)
          group.setPath(area)
          ### good for testing:
          # group.setPen(Qt.red)
          # group.setBrush(QBrush(QColor(255,0,0,50)))
          newgroup = QGraphicsPathItem()
          newgroup.setPen(noPen)
          group.setParentItem(newgroup)
          group = newgroup
        else:
          if (simplify > 0 or smoothen) and tool == rm.FINELINER_TOOL:
            pen.setWidthF(k.width)
            if simplify > 0:
              sk = simpl(k, simplify)
            else:
              sk = k.segments
            path = QPainterPath(QPointF(sk[0][0], sk[0][1]))
            if len(sk) == 2:
              path.lineTo(sk[1][0],sk[1][1])
            elif smoothen:
              px1, px2 = bezierInterpolation(sk, 0)
              py1, py2 = bezierInterpolation(sk, 1)
              for i in range(1,len(sk)):
                path.cubicTo(px1[i-1],py1[i-1],px2[i-1],py2[i-1],sk[i][0],sk[i][1])
            else:
              for i in range(1,len(sk)):
                path.lineTo(sk[i][0],sk[i][1])
            item=QGraphicsPathItem(path, group)
            item.setPen(pen)
          else:
            # STANDARD
            path = QPainterPath(QPointF(k.segments[0].x, k.segments[0].y))
            path.setFillRule(Qt.WindingFill)
            for (w,p), segments in groupby(k.segments[1:], calcwidth):
              for s in segments:
                path.lineTo(s.x,s.y)

              if pencil_resolution and tool == rm.PENCIL_TOOL and p:
                # draw fuzzy edges
                item=QGraphicsPathItem(path, group)
                pen.setBrush(pencilBrushes().getBrush(int(p*.7), scale=pencil_resolution))
                pen.setWidthF(w*1.15)
                item.setPen(pen)

              pen.setWidthF(w)
              if p is not None:
                if pencil_resolution:
                  pen.setBrush(pencilBrushes().getBrush(p, scale=pencil_resolution))
                else:
                  pen.setColor(QColor(int(p*255),int(p*255),int(p*255)))
              item=QGraphicsPathItem(path, group)
              item.setPen(pen)
              path = QPainterPath(path.currentPosition())
              path.setFillRule(Qt.WindingFill)
            # END STANDARD

        _progress(progress,curStroke,totalStrokes); curStroke += 1

      group.setParentItem(self)


_TEMPLATE_CACHE = {}

# TODO:
#   from PyQt5.QtSvg import QSvgRenderer
#   cache r=QSvgRenderer(svgfile)
#   and use i=QGraphicsSvgItem(); i.setSharedRenderer(r)
def pixmapOfBackground(bg):
  if bg and bg.name not in _TEMPLATE_CACHE:
    bgf = bg.retrieve()
    if bgf:
      _TEMPLATE_CACHE[bg.name] = QPixmap.fromImage(QImage(bgf))
    else:
      return None
  return _TEMPLATE_CACHE[bg.name]

def BarePageScene(page, parent=None, include_base_layer=True, orientation=None, **kw):
  scene = QGraphicsScene(parent=parent)
  r = scene.addRect(0,0,rm.WIDTH, rm.HEIGHT)
  r.setFlag(QGraphicsItem.ItemClipsChildrenToShape)
  if page.background and page.background.name != "Blank" and include_base_layer:
    img = pixmapOfBackground(page.background)
    if img:
      QGraphicsPixmapItem(img, r)
  PageGraphicsItem(page, parent=r, **kw)
  scene.setSceneRect(r.rect())
  return scene

# parallelising BarePageScene requires work because Pixmaps can only be created in main thread
# so you need to do as much as possible in the worker thread, get a signal with the pagescene
# and a QImage of the background (maybe), then in main thread you add the PixmapItem to the scene


class ThumbnailSignal(QObject):
  thumbReady = pyqtSignal(str, QImage)

class ThumbnailWorker(QRunnable):

  def __init__(self, index, uid, height=150):
    QRunnable.__init__(self)
    self.uid = uid
    self.index = index
    self.height = height
    self.signals = ThumbnailSignal()

  def run(self):
    painter = None
    try:
      d = self.index.get(self.uid)
      log.debug("Generating thumb for %s", d.name())
      page = d.getPage(d.cover())
      s = BarePageScene(page,
                        include_base_layer=False,
                        pencil_resolution = 1,
                        simplify=0, smoothen=False,
                        eraser_mode=IGNORE_ERASER)
      img = QImage(self.height * s.width() / s.height(), self.height ,QImage.Format_ARGB32)
      img.fill(Qt.white)
      painter = QPainter(img)
      painter.setRenderHint(QPainter.Antialiasing)
      painter.setRenderHint(QPainter.SmoothPixmapTransform)
      if page.background and page.background.name != "Blank":
        bgf = page.background.retrieve()
        if bgf:
          bg = QImage(bgf)
          painter.drawImage(img.rect(), bg)
      else:
        pdf = d.baseDocument()
        if pdf:
          with pdf.lock:
            pdf = pdf.page(d.cover())
            sz = pdf.pageSize()
            w, h = sz.width(), sz.height()
            if w <= h:
              ratio = min(WIDTH / w, HEIGHT / h)
            else:
              ratio = min(HEIGHT / w, WIDTH / h)
            xres = 5.0 * ratio
            yres = 5.0 * ratio
            if w <= h:
              pdf = pdf.renderToImage(xres, yres)
            else:
              pdf = pdf.renderToImage(xres, yres, -1,-1,-1,-1, pdf.Rotate270)
            painter.drawImage(img.rect(), pdf)
      s.render(painter)
      pen = QPen(Qt.gray)
      pen.setWidth(2)
      painter.setPen(pen)
      painter.drawRect(img.rect())
      self.signals.thumbReady.emit(self.uid, img)
    except Exception as e:
      log.warning("Could not create thumbnail for %s [%s]", self.uid, e)
    finally:
      if painter:
        painter.end()
