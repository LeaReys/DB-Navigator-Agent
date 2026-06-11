"""
Делает корень проекта импортируемым из тестов, как бы pytest ни был запущен.
Сам факт наличия conftest.py в корне добавляет эту папку в sys.path,
а явная вставка ниже страхует от нестандартных режимов импорта.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
