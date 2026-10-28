"""GitLab 토큰 조회/저장. 설정 파일에는 토큰을 절대 저장하지 않는다.

조회 순서: 환경변수(gitlab.token_env, 기본 GITLAB_TOKEN) > OS 키링(Windows 자격 증명 관리자)
"""
from __future__ import annotations

import os
from typing import Optional

from shipkit import CLI_NAME
from shipkit.config import GitLabConfig

KEYRING_SERVICE = CLI_NAME


class SecretError(Exception):
    pass


def _keyring():
    try:
        import keyring  # type: ignore
    except ImportError:
        return None
    return keyring


def get_token(cfg: GitLabConfig) -> str:
    token = os.environ.get(cfg.token_env, "").strip()
    if token:
        return token
    kr = _keyring()
    if kr is not None:
        try:
            token = (kr.get_password(KEYRING_SERVICE, cfg.url) or "").strip()
        except Exception as e:  # 키링 백엔드 오류는 원인 그대로 알려준다
            raise SecretError(f"키링에서 토큰을 읽지 못했습니다: {e}") from e
        if token:
            return token
    hint = f"'{CLI_NAME} token set'" if kr is not None else f"keyring 패키지 설치 후 '{CLI_NAME} token set'"
    raise SecretError(f"GitLab 토큰이 없습니다. 환경변수 {cfg.token_env} 를 설정하거나 {hint} 으로 저장하세요.")


def set_token(cfg: GitLabConfig, token: str) -> None:
    kr = _require_keyring()
    kr.set_password(KEYRING_SERVICE, cfg.url, token)


def delete_token(cfg: GitLabConfig) -> bool:
    kr = _require_keyring()
    try:
        kr.delete_password(KEYRING_SERVICE, cfg.url)
        return True
    except Exception:
        return False


def token_source(cfg: GitLabConfig) -> Optional[str]:
    if os.environ.get(cfg.token_env, "").strip():
        return f"환경변수 {cfg.token_env}"
    kr = _keyring()
    if kr is not None and kr.get_password(KEYRING_SERVICE, cfg.url):
        return "OS 키링"
    return None


def _require_keyring():
    kr = _keyring()
    if kr is None:
        raise SecretError("keyring 패키지가 필요합니다: pip install keyring")
    return kr
