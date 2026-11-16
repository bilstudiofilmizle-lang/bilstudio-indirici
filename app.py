"""
BİLSTUDİO Video İndirici - Sunucu (Flask + yt-dlp)

Arayüz (index.html) Netlify'da durur, bu sunucu ayrı bir yerde çalışır.
Uç noktalar:
    POST /api/info          {url}        -> başlık, kapak, çözünürlükler
    POST /api/prepare       {url, q}     -> indirmeyi başlatır, {id} döner
    GET  /api/status/<id>                -> ilerleme (yüzde, hız, durum)
    GET  /api/file/<id>                  -> hazır dosyayı verir

Ortam değişkenleri (isteğe bağlı):
    ALLOWED_ORIGIN   sadece senin siten erişsin: https://bilstudio.netlify.app
    MAX_CONCURRENT   aynı anda indirme sayısı (varsayılan 3)
    RATE_LIMIT       IP başına dakikada istek (varsayılan 10)
    MAX_FILESIZE_MB  dosya boyutu sınırı (varsayılan 4096)
"""
import ipaddress
import json
import os
import re
import shutil
import socket
