import json

import requests
from PyQt5.QtCore import QDir, Qt, QTemporaryFile
from PyQt5.QtGui import QImage, QPainter

import remedy.remarkable.constants as rm
from remedy.remarkable.render import BarePageScene
from remedy.utils import log

try:
    from simplification.cutil import simplify_coords

    def simpl(stroke):
        return simplify_coords([[s.x, s.y] for s in stroke.segments], 2.0)

except Exception:
    simpl = None


class MathPixError(Exception):
    def __init__(self, result):
        Exception.__init__(self, result.get('error'))
        self.result = result


def mathpixRaster(page, app_id, app_key, scale=0.5, **opt):
    s = BarePageScene(page, **opt)
    img = QImage(scale * rm.WIDTH, scale * rm.HEIGHT, QImage.Format_RGB32)
    img.fill(Qt.GlobalColor.white)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.Antialiasing)
    s.render(painter)
    painter.end()
    temp = QTemporaryFile(QDir.tempPath() + '/XXXXXX.jpg')
    img.save(temp)
    ## debug:
    # QDesktopServices.openUrl(QUrl("file://" + temp.fileName()))
    r = requests.post(
        'https://api.mathpix.com/v3/text',
        files={'file': open(temp.fileName(), 'rb')},
        data={
            'options_json': json.dumps(
                {
                    'formats': ['text'],
                    'math_inline_delimiters': ['$', '$'],
                }
            )
        },
        headers={'app_id': app_id, 'app_key': app_key},
    )
    result = r.json()
    if 'error' in result:
        i = result['error_info']['id']
        if i == 'image_no_content':
            return {'text': ''}
        raise MathPixError(result)
    return result


### TODO: Use rendered output since Max request size is 5mb for images and 512kb for strokes
