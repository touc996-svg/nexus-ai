"""Request builders. Each scenario is ``async def step(ctx) -> httpx.Response`` where ``ctx`` identifies a virtual user."""

import io
import random
import zipfile
from dataclasses import dataclass, field

import httpx

TOPICS = ["quarterly planning", "incident response", "vendor risk", "onboarding checklist", "budget review",
          "network segmentation", "study schedule", "phishing triage", "capacity forecast", "access review"]


@dataclass
class Ctx:
    client: httpx.AsyncClient
    user: str
    session: str
    rng: random.Random = field(default_factory=random.Random)
    counter: int = 0

    def next_topic(self) -> str:
        self.counter += 1
        return f"{self.rng.choice(TOPICS)} {self.rng.choice(TOPICS)} {self.counter}"


def text_document(ctx: Ctx, paragraphs: int = 12) -> bytes:
    body = "\n\n".join(f"Paragraph {i} about {ctx.next_topic()}. " + ("Operational detail sentence. " * 8)
                       for i in range(paragraphs))
    return body.encode("utf-8")


def docx_document(ctx: Ctx, paragraphs: int = 400) -> bytes:
    """A minimal valid .docx built with the standard library (no python-docx needed on the load generator)."""
    text = "".join(f"<w:p><w:r><w:t>{ctx.next_topic()} lorem ipsum dolor sit amet consectetur</w:t></w:r></w:p>"
                   for _ in range(paragraphs))
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
                f'wordprocessingml/2006/main"><w:body>{text}</w:body></w:document>')
    content_types = ('<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/'
                     'content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships'
                     '+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" '
                     'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            'relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
            'officeDocument" Target="word/document.xml"/></Relationships>')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", rels)
        archive.writestr("word/document.xml", document)
    return buffer.getvalue()


async def health(ctx: Ctx):
    return await ctx.client.get("/health/live")


async def ready(ctx: Ctx):
    return await ctx.client.get("/health/ready")


async def chat(ctx: Ctx):
    return await ctx.client.post("/chat", json={"message": f"Please summarise {ctx.next_topic()}",
                                                "session_id": ctx.session, "user_id": ctx.user})


async def agent(ctx: Ctx):
    return await ctx.client.post("/agents/enterprise", json={"message": f"Analyse {ctx.next_topic()}"})


async def team(ctx: Ctx):
    return await ctx.client.post("/team", json={"message": f"Plan and assess {ctx.next_topic()}"})


async def ingest(ctx: Ctx):
    files = {"file": (f"doc-{ctx.user}-{ctx.counter}.txt", text_document(ctx), "text/plain")}
    return await ctx.client.post("/knowledge/ingest", files=files, data={"user_id": ctx.user})


async def ingest_docx(ctx: Ctx):
    files = {"file": (f"big-{ctx.user}-{ctx.counter}.docx", docx_document(ctx),
                      "application/vnd.openxmlformats-officedocument.wordprocessingml.document")}
    return await ctx.client.post("/knowledge/ingest", files=files, data={"user_id": ctx.user})


async def search(ctx: Ctx):
    return await ctx.client.post("/knowledge/search", json={"query": ctx.next_topic(), "limit": 5, "user_id": ctx.user})


async def ratelimit(ctx: Ctx):
    return await ctx.client.get("/system/alerts")


MIX = [(health, 20), (search, 30), (chat, 30), (ingest, 10), (agent, 5), (team, 5)]


async def mixed(ctx: Ctx):
    steps, weights = zip(*MIX, strict=True)
    return await ctx.rng.choices(steps, weights)[0](ctx)


SCENARIOS = {fn.__name__: fn for fn in (health, ready, chat, agent, team, ingest, ingest_docx, search, ratelimit, mixed)}
