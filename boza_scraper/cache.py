# cache.py
import diskcache

from config import CACHE_DIR

cache = diskcache.Cache(CACHE_DIR)
