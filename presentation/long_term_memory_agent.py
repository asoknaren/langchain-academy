"""Task assistant with long-term memory (profile, ToDo collection, instructions).

Standalone version of module-5/memory_agent.ipynb.

Setup:  pip install -U langgraph langchain_openai langchain_core trustcall pydantic
Run:    set OPENAI_API_KEY=...  then  python long_term_memory_agent.py [user_id]
Commands inside the chat: /thread N  switch thread (same user -> same long-term memory)
                          /store     dump long-term memory
                          /quit      exit
"""
import json
import sys
import uuid
from datetime import datetime
from typing import Literal, Optional, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage, merge_message_runs
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore
from pydantic import BaseModel, Field
from trustcall import create_extractor

model = ChatOpenAI(model="gpt-4o", temperature=0)


class Profile(BaseModel):
    """Profile of the user you are chatting with."""
    name: Optional[str] = Field(description="The user's name", default=None)
    location: Optional[str] = Field(description="The user's location", default=None)
    job: Optional[str] = Field(description="The user's job", default=None)
    connections: list[str] = Field(description="Family, friends or coworkers", default_factory=list)
    interests: list[str] = Field(description="Interests of the user", default_factory=list)


class ToDo(BaseModel):
    task: str = Field(description="The task to be completed.")
    time_to_complete: Optional[int] = Field(description="Estimated minutes to complete.", default=None)
    deadline: Optional[datetime] = Field(description="Deadline, if any.", default=None)
    solutions: list[str] = Field(description="Concrete, actionable ideas for completing the task.", default_factory=list)
    status: Literal["not started", "in progress", "done", "archived"] = Field(default="not started")


class UpdateMemory(TypedDict):
    """Decision on which memory type to update."""
    update_type: Literal["user", "todo", "instructions"]


profile_extractor = create_extractor(model, tools=[Profile], tool_choice="Profile")
todo_extractor = create_extractor(model, tools=[ToDo], tool_choice="ToDo", enable_inserts=True)

MODEL_SYSTEM_MESSAGE = """You are a helpful assistant that keeps track of the user's ToDo list.

You have a long term memory with three parts:
1. The user's profile
2. The user's ToDo list
3. Instructions for updating the ToDo list

<user_profile>
{user_profile}
</user_profile>

<todo>
{todo}
</todo>

<instructions>
{instructions}
</instructions>

Rules:
- If the user shares personal information, call UpdateMemory with 'user'.
- If the user mentions tasks, call UpdateMemory with 'todo'.
- If the user states preferences on how to manage the ToDo list, call UpdateMemory with 'instructions'.
- Tell the user when you updated the ToDo list, but do not mention profile or instruction updates.
- Err on the side of updating the ToDo list; no need to ask for permission.
- Respond naturally once any tool call has completed."""

TRUSTCALL_INSTRUCTION = "Reflect on the following interaction and update/create memories accordingly. Time: {time}"

CREATE_INSTRUCTIONS = """Reflect on the interaction and the current instructions for managing the ToDo list.

<current_instructions>
{current_instructions}
</current_instructions>

Update them only based on explicit user feedback about how to add ToDos. Return only the updated instructions text."""


def _cfg(config: RunnableConfig) -> str:
    return config["configurable"]["user_id"]


def task_mAIstro(state: MessagesState, config: RunnableConfig, store: BaseStore):
    user_id = _cfg(config)
    profile = store.search(("profile", user_id))
    user_profile = profile[0].value if profile else None
    todo = "\n".join(json.dumps(i.value, default=str) for i in store.search(("todo", user_id)))
    inst = store.search(("instructions", user_id))
    instructions = inst[0].value["memory"] if inst else ""

    system = MODEL_SYSTEM_MESSAGE.format(user_profile=user_profile, todo=todo, instructions=instructions)
    response = model.bind_tools([UpdateMemory], parallel_tool_calls=False).invoke(
        [SystemMessage(content=system)] + state["messages"]
    )
    return {"messages": [response]}


def update_profile(state: MessagesState, config: RunnableConfig, store: BaseStore):
    user_id = _cfg(config)
    namespace = ("profile", user_id)
    existing = store.search(namespace)
    existing_memories = [(i.key, "Profile", i.value) for i in existing] if existing else None

    instruction = TRUSTCALL_INSTRUCTION.format(time=datetime.now().isoformat())
    msgs = list(merge_message_runs([SystemMessage(content=instruction)] + state["messages"][:-1]))
    result = profile_extractor.invoke({"messages": msgs, "existing": existing_memories})
    for r, meta in zip(result["responses"], result["response_metadata"]):
        store.put(namespace, meta.get("json_doc_id", str(uuid.uuid4())), r.model_dump(mode="json"))
    return _ack(state, "updated profile")


def update_todos(state: MessagesState, config: RunnableConfig, store: BaseStore):
    user_id = _cfg(config)
    namespace = ("todo", user_id)
    existing = store.search(namespace)
    existing_memories = [(i.key, "ToDo", i.value) for i in existing] if existing else None

    instruction = TRUSTCALL_INSTRUCTION.format(time=datetime.now().isoformat())
    msgs = list(merge_message_runs([SystemMessage(content=instruction)] + state["messages"][:-1]))
    result = todo_extractor.invoke({"messages": msgs, "existing": existing_memories})
    for r, meta in zip(result["responses"], result["response_metadata"]):
        store.put(namespace, meta.get("json_doc_id", str(uuid.uuid4())), r.model_dump(mode="json"))
    return _ack(state, "updated todo list")


def update_instructions(state: MessagesState, config: RunnableConfig, store: BaseStore):
    user_id = _cfg(config)
    namespace = ("instructions", user_id)
    existing = store.get(namespace, "user_instructions")
    current = existing.value["memory"] if existing else ""

    msgs = [SystemMessage(content=CREATE_INSTRUCTIONS.format(current_instructions=current))] + state["messages"][:-1]
    new = model.invoke(msgs)
    store.put(namespace, "user_instructions", {"memory": new.content})
    return _ack(state, "updated instructions")


def _ack(state: MessagesState, content: str):
    call = state["messages"][-1].tool_calls[0]
    return {"messages": [ToolMessage(content=content, tool_call_id=call["id"])]}


def route(state: MessagesState) -> Literal[END, "update_todos", "update_instructions", "update_profile"]:
    calls = state["messages"][-1].tool_calls
    if not calls:
        return END
    return {"user": "update_profile", "todo": "update_todos", "instructions": "update_instructions"}[
        calls[0]["args"]["update_type"]
    ]


def build_graph(store: BaseStore):
    b = StateGraph(MessagesState)
    b.add_node(task_mAIstro)
    b.add_node(update_todos)
    b.add_node(update_profile)
    b.add_node(update_instructions)
    b.add_edge(START, "task_mAIstro")
    b.add_conditional_edges("task_mAIstro", route)
    for n in ("update_todos", "update_profile", "update_instructions"):
        b.add_edge(n, "task_mAIstro")
    return b.compile(checkpointer=MemorySaver(), store=store)


def dump_store(store: BaseStore, user_id: str):
    for ns in ("profile", "todo", "instructions"):
        print(f"--- {ns}")
        for item in store.search((ns, user_id)):
            print(json.dumps(item.value, indent=2, default=str))


def main():
    user_id = sys.argv[1] if len(sys.argv) > 1 else "naren"
    store = InMemoryStore()
    graph = build_graph(store)
    thread = 1
    print(f"user_id={user_id}, thread={thread}. Type /thread N, /store, /quit")
    while True:
        text = input("you> ").strip()
        if not text:
            continue
        if text == "/quit":
            break
        if text == "/store":
            dump_store(store, user_id)
            continue
        if text.startswith("/thread"):
            thread = int(text.split()[1])
            print(f"switched to thread {thread}")
            continue
        config = {"configurable": {"user_id": user_id, "thread_id": str(thread)}}
        result = graph.invoke({"messages": [HumanMessage(content=text)]}, config)
        print("bot>", result["messages"][-1].content)


if __name__ == "__main__":
    main()
