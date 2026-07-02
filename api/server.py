"""FastAPI server entrypoint for Morgoth."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from agents.agent_manager import AgentManager
from api.routes import (
    admin,
    agents,
    brain,
    chat,
    consciousness,
    evolution,
    knowledge,
    market,
    objectives,
    tools as tools_route,
    wiki,
)
from api.ws.handler import InboundWebSocketMessage, WebSocketManager
from core.brain import Brain
from core.config import AppConfig, load_config
from core.llm_client import OllamaLLMClient
from core.scheduler import Scheduler
from core.tool_router import ToolRouter
from memory.episodic import EpisodicMemory
from memory.persistent import PersistentMemory
from notifications.telegram import TelegramNotifier
from tools.agent_control import CreateAgentTool
from tools.analysis.technical import TechnicalAnalysisTool
from tools.connectors.fred import FredSeriesObservationsTool, FredSeriesSearchTool
from tools.connectors.reddit import RedditSearchTool, RedditSubredditPostsTool
from tools.code_executor import ExecutePythonTool
from tools.discovery import discover_data_feed_tools, instantiate_tool
from tools.file_manager import ReadFileTool, WriteFileTool
from tools.memory_tools import RecallTool, RememberTool
from tools.objectives_tool import CreateObjectiveTool, UpdateObjectiveTool
from tools.notifications import NotifyTool
from tools.web_search import WebSearchTool


def build_tool_router(
    config: AppConfig,
    persistent_memory: PersistentMemory,
    episodic_memory: EpisodicMemory,
    agent_manager: AgentManager,
    notifier: TelegramNotifier,
) -> ToolRouter:
    """Register all Layer 1 tools and return the router."""

    router = ToolRouter()
    router.register(WebSearchTool(config))
    router.register(ExecutePythonTool(config))
    router.register(ReadFileTool(config))
    router.register(WriteFileTool(config))
    # Auto-discovery: replaces the previous hand-registration of the four
    # data_feeds tools. A new file under tools/data_feeds/ is picked up at
    # process start; no red-zone edit required.
    for cls in discover_data_feed_tools():
        router.register(instantiate_tool(cls, config, persistent_memory))
    router.register(FredSeriesSearchTool(config))
    router.register(FredSeriesObservationsTool(config))
    router.register(RedditSearchTool(config))
    router.register(RedditSubredditPostsTool(config))
    router.register(TechnicalAnalysisTool())
    router.register(CreateAgentTool(config, agent_manager))
    router.register(NotifyTool(config, notifier))
    router.register(RememberTool(episodic_memory))
    router.register(RecallTool(episodic_memory))
    router.register(CreateObjectiveTool(persistent_memory))
    router.register(UpdateObjectiveTool(persistent_memory))
    return router


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Initialize and tear down the application state."""

    config = await load_config()
    llm_client = OllamaLLMClient(config)
    persistent_memory = PersistentMemory(config)
    episodic_memory = EpisodicMemory(config.chroma_dir)
    scheduler = Scheduler(persistent_memory)
    notifier = TelegramNotifier(config)
    websocket_manager = WebSocketManager()
    agent_manager = AgentManager(config, llm_client, persistent_memory)
    tool_router = build_tool_router(config, persistent_memory, episodic_memory, agent_manager, notifier)
    brain_service = Brain(
        config,
        llm_client,
        persistent_memory,
        episodic_memory,
        scheduler,
        tool_router,
        agent_manager,
        notifier,
        websocket_manager,
    )
    await brain_service.initialize()

    app.state.config = config
    app.state.llm_client = llm_client
    app.state.persistent_memory = persistent_memory
    app.state.episodic_memory = episodic_memory
    app.state.scheduler = scheduler
    app.state.notifier = notifier
    app.state.websocket_manager = websocket_manager
    app.state.agent_manager = agent_manager
    app.state.tool_router = tool_router
    app.state.brain = brain_service

    try:
        yield
    finally:
        await brain_service.shutdown()


app = FastAPI(title="Morgoth", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(agents.router)
app.include_router(market.router)
app.include_router(brain.router)
app.include_router(consciousness.router)
app.include_router(objectives.router)
app.include_router(evolution.router)
app.include_router(admin.router)
app.include_router(knowledge.router)
app.include_router(wiki.router)
app.include_router(tools_route.router)


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket) -> None:
    """Bidirectional chat websocket."""

    manager: WebSocketManager = websocket.app.state.websocket_manager
    await manager.connect(websocket)
    try:
        while True:
            payload = InboundWebSocketMessage.model_validate(await websocket.receive_json())
            if payload.type == "chat":
                await websocket.app.state.brain.process_message(payload.content, payload.user_id)
            else:
                await websocket.app.state.brain.broadcast("system", payload.content, metadata={"command": True})
    except WebSocketDisconnect:
        await manager.disconnect(websocket)
