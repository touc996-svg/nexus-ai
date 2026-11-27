"""Fixtures for the HTTP-level security suite (helpers are in helpers.py)."""

import logging
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main
from app.api import knowledge
from app.core import conversation as conversation_module
from app.core.database import Base
from app.core.memory import NexusMemory
from app.core.request_observability import install_observability
from app.security.hardening import install_security
from app.security.keystore import KeyStore
from app.services import profile_learning as profile_learning_module
from app.services.rag import NexusRAG
from tests.security.helpers import (
    ALICE_KEY,
    BOB_KEY,
    ENV_NAMES,
    JWT_AUDIENCE,
    JWT_ISSUER,
    JWT_SECRET,
    OPERATOR_KEY,
    Clock,
    FakeLLM,
    _NullActivity,
    bow_embed,
)


@pytest.fixture
def make_app(monkeypatch):
    """Factory: ``make_app(env={...}, jwt=False, peer="198.51.100.10")`` -> namespace with client, memory, rag, llm, store, clock."""

    def build(env=None, *, jwt=False, peer="198.51.100.10", keys=True):
        for name in ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("NEXUS_RATE_LIMIT_PER_MINUTE", "100000")
        monkeypatch.setenv("NEXUS_RATE_LIMIT_PER_IDENTITY_PER_MINUTE", "100000")
        if keys:
            monkeypatch.setenv("NEXUS_API_KEY", OPERATOR_KEY)
            monkeypatch.setenv("NEXUS_API_KEYS", f"{ALICE_KEY}=alice,{BOB_KEY}=bob")
        if jwt:
            monkeypatch.setenv("NEXUS_JWT_SECRETS", JWT_SECRET)
            monkeypatch.setenv("NEXUS_JWT_ISSUER", JWT_ISSUER)
            monkeypatch.setenv("NEXUS_JWT_AUDIENCE", JWT_AUDIENCE)
        for name, value in (env or {}).items():
            monkeypatch.setenv(name, str(value))

        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(bind=engine)
        factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
        clock = Clock()
        memory = NexusMemory(max_messages=50, history_limit=20, session_factory=factory, initialize=False)
        store = KeyStore(session_factory=factory, clock=clock, initialize=False)
        rag = NexusRAG(client=QdrantClient(location=":memory:"), demo_mode=False, score_threshold=0.0)
        monkeypatch.setattr(rag, "embed", bow_embed)
        llm = FakeLLM()

        monkeypatch.setattr(main, "DEMO_MODE", False)
        monkeypatch.setattr(main, "memory", memory)
        monkeypatch.setattr(main, "rag", rag)
        monkeypatch.setattr(knowledge, "rag", rag)
        monkeypatch.setattr(conversation_module, "client", llm)
        monkeypatch.setattr(profile_learning_module, "client", llm)
        monkeypatch.setattr(main.nexus, "activity_sink", _NullActivity())
        for agent in main.nexus.agents.values():
            monkeypatch.setattr(agent, "client", llm)

        api = FastAPI()
        api.router.routes.extend(main.app.router.routes)  # the production routes
        install_security(api, key_store=store)
        install_observability(api)
        client = TestClient(api, client=(peer, 5000), raise_server_exceptions=False)
        return SimpleNamespace(client=client, memory=memory, rag=rag, llm=llm, store=store, clock=clock, api=api)

    return build


@pytest.fixture
def app_ctx(make_app):
    return make_app()


@pytest.fixture(autouse=True)
def quiet_application_logs():
    """The suite provokes many deliberate errors; keep their log lines out of the test output."""
    logger = logging.getLogger("nexus")
    previous = logger.level
    logger.setLevel(logging.CRITICAL)
    yield
    logger.setLevel(previous)
