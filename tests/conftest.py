# -*- coding: utf-8 -*-
"""测试基线钉中文:大量断言直接匹配中文文案,不能随开发机 locale 漂移。
英文文案有专门的 en 模式用例(显式改 FINEPRINT_LANG)。"""
import os

os.environ.setdefault("FINEPRINT_LANG", "zh")
