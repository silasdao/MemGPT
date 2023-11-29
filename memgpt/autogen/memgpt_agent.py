from autogen.agentchat import ConversableAgent, Agent
from ..agent import AgentAsync

import asyncio
from typing import Callable, Optional, List, Dict, Union, Any, Tuple

from .interface import AutoGenInterface
from ..persistence_manager import InMemoryStateManager
from .. import system
from .. import constants
from .. import presets
from ..personas import personas
from ..humans import humans


def create_memgpt_autogen_agent_from_config(
    name: str,
    system_message: Optional[str] = "You are a helpful AI Assistant.",
    is_termination_msg: Optional[Callable[[Dict], bool]] = None,
    max_consecutive_auto_reply: Optional[int] = None,
    human_input_mode: Optional[str] = "TERMINATE",
    function_map: Optional[Dict[str, Callable]] = None,
    code_execution_config: Optional[Union[Dict, bool]] = None,
    llm_config: Optional[Union[Dict, bool]] = None,
    default_auto_reply: Optional[Union[str, Dict, None]] = "",
):
    """
    TODO support AutoGen config workflow in a clean way with constructors
    """
    raise NotImplementedError


def create_autogen_memgpt_agent(
    autogen_name,
    preset=presets.DEFAULT,
    model=constants.DEFAULT_MEMGPT_MODEL,
    persona_description=personas.DEFAULT,
    user_description=humans.DEFAULT,
    interface=None,
    interface_kwargs={},
    persistence_manager=None,
    persistence_manager_kwargs={},
):
    """
    See AutoGenInterface.__init__ for available options you can pass into
    `interface_kwargs`.  For example, MemGPT's inner monologue and functions are
    off by default so that they are not visible to the other agents. You can
    turn these on by passing in
    ```
    interface_kwargs={
        "debug": True,  # to see all MemGPT activity
        "show_inner_thoughts: True  # to print MemGPT inner thoughts "globally"
                                    # (visible to all AutoGen agents)
    }
    ```
    """
    interface = AutoGenInterface(**interface_kwargs) if interface is None else interface
    persistence_manager = InMemoryStateManager(**persistence_manager_kwargs) if persistence_manager is None else persistence_manager

    memgpt_agent = presets.use_preset(
        preset,
        model,
        persona_description,
        user_description,
        interface,
        persistence_manager,
    )

    return MemGPTAgent(
        name=autogen_name,
        agent=memgpt_agent,
    )


class MemGPTAgent(ConversableAgent):
    def __init__(
        self,
        name: str,
        agent: AgentAsync,
        skip_verify=False,
        concat_other_agent_messages=False,
    ):
        super().__init__(name)
        self.agent = agent
        self.skip_verify = skip_verify
        self.concat_other_agent_messages = concat_other_agent_messages
        self.register_reply([Agent, None], MemGPTAgent._a_generate_reply_for_user_message)
        self.register_reply([Agent, None], MemGPTAgent._generate_reply_for_user_message)
        self.messages_processed_up_to_idx = 0

    def format_other_agent_message(self, msg):
        return f"{msg['name']}: {msg['content']}" if "name" in msg else msg["content"]

    def find_last_user_message(self):
        last_user_message = None
        for msg in self.agent.messages:
            if msg["role"] == "user":
                last_user_message = msg["content"]
        return last_user_message

    def find_new_messages(self, entire_message_list):
        """Extract the subset of messages that's actually new"""
        return entire_message_list[self.messages_processed_up_to_idx :]

    def _generate_reply_for_user_message(
        self,
        messages: Optional[List[Dict]] = None,
        sender: Optional[Agent] = None,
        config: Optional[Any] = None,
    ) -> Tuple[bool, Union[str, Dict, None]]:
        return asyncio.run(self._a_generate_reply_for_user_message(messages=messages, sender=sender, config=config))

    async def _a_generate_reply_for_user_message(
        self,
        messages: Optional[List[Dict]] = None,
        sender: Optional[Agent] = None,
        config: Optional[Any] = None,
    ) -> Tuple[bool, Union[str, Dict, None]]:
        self.agent.interface.reset_message_list()

        new_messages = self.find_new_messages(messages)
        if len(new_messages) > 1:
            if self.concat_other_agent_messages:
                # Combine all the other messages into one message
                user_message = "\n".join([self.format_other_agent_message(m) for m in new_messages])
            else:
                # Extend the MemGPT message list with multiple 'user' messages, then push the last one with agent.step()
                self.agent.messages.extend(new_messages[:-1])
                user_message = new_messages[-1]
        elif len(new_messages) == 1:
            user_message = new_messages[0]
        else:
            return True, self._default_auto_reply

        # Package the user message
        user_message = system.package_user_message(user_message)

        # Send a single message into MemGPT
        while True:
            (
                new_messages,
                heartbeat_request,
                function_failed,
                token_warning,
            ) = await self.agent.step(user_message, first_message=False, skip_verify=self.skip_verify)
            # Skip user inputs if there's a memory warning, function execution failed, or the agent asked for control
            if token_warning:
                user_message = system.get_token_limit_warning()
            elif function_failed:
                user_message = system.get_heartbeat(constants.FUNC_FAILED_HEARTBEAT_MESSAGE)
            elif heartbeat_request:
                user_message = system.get_heartbeat(constants.REQ_HEARTBEAT_MESSAGE)
            else:
                break

        # Pass back to AutoGen the pretty-printed calls MemGPT made to the interface
        pretty_ret = MemGPTAgent.pretty_concat(self.agent.interface.message_list)
        self.messages_processed_up_to_idx += len(new_messages)
        return True, pretty_ret

    @staticmethod
    def pretty_concat(messages):
        """AutoGen expects a single response, but MemGPT may take many steps.

        To accommodate AutoGen, concatenate all of MemGPT's steps into one and return as a single message.
        """
        lines = [f"{m}" for m in messages]
        return {"role": "assistant", "content": "\n".join(lines)}
