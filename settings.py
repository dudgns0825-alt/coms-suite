# -*- coding: utf-8 -*-
"""
설정 파일 (config.txt) 읽기·쓰기
================================================

인증정보는 소스에 넣지 않고 프로그램 옆의 config.txt 에 둔다.
두 탭이 같은 파일을 쓰므로 여기 한 곳에서만 다룬다.

  api_key       DART OpenAPI 인증키 (40자리)
  edgar_contact SEC가 요구하는 '이름 이메일'. 인증키가 아니라 신원 표시다.

옛 파일도 그대로 읽힌다.
  · 다운로더가 쓰던 `contact =` 는 `edgar_contact` 로 취급한다.
  · 인증키 한 줄만 적혀 있던 최초 형식은 그 줄을 인증키로 본다.
"""

import os
import re


CONFIG_FILE = "config.txt"

# 넣어 둔 예시 문구를 값으로 착각하지 않으려고 걸러 내는 표시
PLACEHOLDER = ("여기에", "이메일주소", "붙여넣으세요")


def config_path(base_dir):
    return os.path.join(base_dir, CONFIG_FILE)


def load_config(base_dir):
    """config.txt 를 {키: 값} 으로 읽는다. 파일이 없으면 빈 dict."""
    path = config_path(base_dir)
    conf = {}
    if not os.path.exists(path):
        return conf

    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                conf[key.strip().lower()] = value.strip()
            elif "api_key" not in conf:
                conf["api_key"] = line      # 인증키 한 줄만 있던 옛 형식

    # 다운로더가 쓰던 이름을 새 이름으로 옮겨 준다
    if "edgar_contact" not in conf and conf.get("contact"):
        conf["edgar_contact"] = conf["contact"]

    return conf


def load_api_key(base_dir):
    """40자리 인증키. 없거나 형식이 다르면 빈 문자열."""
    key = load_config(base_dir).get("api_key", "").strip()
    return key if re.fullmatch(r"[0-9a-zA-Z]{40}", key) else ""


def load_edgar_contact(base_dir):
    """
    EDGAR 연락처. 예시 문구를 지우지 않은 경우는 없는 것으로 본다
    (그대로 보내면 SEC가 403으로 거절한다).
    """
    contact = load_config(base_dir).get("edgar_contact", "").strip()
    if not contact or "@" not in contact:
        return ""
    if any(mark in contact for mark in PLACEHOLDER):
        return ""
    return contact


def save_config(base_dir, api_key=None, edgar_contact=None):
    """건드린 항목만 바꾸고 나머지는 그대로 둔다."""
    conf = load_config(base_dir)
    if api_key is not None:
        conf["api_key"] = api_key.strip()
    if edgar_contact is not None:
        conf["edgar_contact"] = edgar_contact.strip()

    with open(config_path(base_dir), "w", encoding="utf-8") as f:
        f.write("# DART OpenAPI 인증키 (https://opendart.fss.or.kr 에서 발급)\n")
        f.write("# 이 파일은 공유하거나 git에 올리지 말 것\n")
        f.write(f"api_key = {conf.get('api_key', '')}\n")
        f.write("\n")
        f.write("# EDGAR(미국 SEC) 연락처. 인증키가 아니라 신원 표시이며,\n")
        f.write("# SEC 규정상 '이름 이메일' 형식으로 넣지 않으면 403으로 거절당한다.\n")
        f.write(f"edgar_contact = {conf.get('edgar_contact', '')}\n")
