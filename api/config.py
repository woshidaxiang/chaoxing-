# -*- coding: utf-8 -*-
import os


class GlobalConst:
    AESKey = "u2oh6Vu^HWe4_AES"

    # Cookie 存储路径。
    # 【重要】必须是「容器内共享卷里的绝对路径」，不能是相对路径：
    #   - 相对路径 "cookies.txt" 会落到进程工作目录（/app），
    #     而数据卷挂在 /app/data —— 容器重建后 Cookie 就丢了，
    #     且 Web 容器与 Worker 容器看到的是各自的 /app（不共享）。
    #   - 用 /app/data/cookies.txt 后，所有容器共享同一份，且随卷持久化。
    # 可用环境变量 CHAOXING_COOKIES 覆盖（如非 Docker 部署时）。
    COOKIES_PATH = os.environ.get("CHAOXING_COOKIES", "/app/data/cookies.txt")

    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
        "sec-ch-ua": '"Chromium";v="118", "Google Chrome";v="118", "Not=A?Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"'
    }
    VIDEO_HEADERS = {
        "Referer": "https://mooc1.chaoxing.com/ananas/modules/video/index.html?v=2025-0725-1842",
    }
    AUDIO_HEADERS = {
        "Referer": "https://mooc1.chaoxing.com/ananas/modules/audio/index_new.html?v=2025-0725-1842",
    }

    THRESHOLD = 1
