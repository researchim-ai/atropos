import base64
import io
import os
import random
import re
import sys
import traceback
from typing import List, Optional, Tuple

import requests
from datasets import load_dataset
from PIL import Image

from atroposlib.envs.base import BaseEnv, BaseEnvConfig, OpenaiConfig, ScoredDataGroup
from atroposlib.type_definitions import GameHistory, Item
from atroposlib.utils.tokenize_for_trainer import tokenize_for_trainer


class PixmoCountEnv(BaseEnv):
    name = "pixmo_count"
    name_config_cls = BaseEnvConfig

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def setup(self):
        # Load the pixmo-count dataset
        self.dataset = load_dataset("allenai/pixmo-count")
        self.train = self.dataset["train"]
        self.iter = 0

    async def get_next_item(self) -> Item:
        try:
            entry = self.train[self.iter % len(self.train)]
            self.iter += 1

            label = entry["label"]
            count = entry["count"]
            question = f"how many {label} are in the image?"
            prompt = tuple([frozenset({"role": "user", "content": question}.items())])

            gold_answer = f"<answer>{count}</answer>"

            # Load image from URL and convert to base64
            image_url = entry["image_url"]
            response = requests.get(image_url)
            img = Image.open(io.BytesIO(response.content))
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            img_bytes = buf.getvalue()
            base64_image = base64.b64encode(img_bytes).decode("utf-8")

            return (prompt, gold_answer, base64_image)
        except Exception:
            traceback.print_exc()
            fallback = tuple(
                [
                    frozenset(
                        {"role": "user", "content": "Please solve: 2 + 2 = ?"}.items()
                    )
                ]
            )
            return (fallback, "<answer>4</answer>", None)

    async def collect_trajectories(
        self, item: Item
    ) -> Tuple[GameHistory | None, List[Item]]:
        to_score: List[Tuple[GameHistory, str, Optional[str]]] = []
        to_backlog: List[Item] = []

        prompt_tuple, gold, base64_image = item
        text_prompt = dict(prompt_tuple[0])["content"]

        system_msg = {
            "role": "system",
            "content": (
                "You must submit your answer enclosed in <answer> tags, "
                "e.g., <answer>3</answer>"
            ),
        }
        user_msg = {
            "role": "user",
            "content": [
                {"type": "text", "text": text_prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{base64_image}"},
                },
            ],
        }
        messages = [system_msg, user_msg]

        chat_completions = await self.server.chat_completion(
            messages=messages,
            n=self.config.group_size,
            max_tokens=512,
            timeout=60,
        )

        for choice in chat_completions.choices:
            user_hist = {"role": "user", "content": text_prompt}
            assistant_hist = {"role": "assistant", "content": choice.message.content}
            history: GameHistory = (user_hist, assistant_hist)
            to_score.append((history, gold, base64_image))

        return to_score, to_backlog

    async def postprocess_histories(
        self, trajectories: List[GameHistory]
    ) -> ScoredDataGroup:
        # No custom post-processing
        pass

    async def evaluate(self, *args, **kwargs):
        # No custom evaluation
        return

    async def score(self, rollout_group_data) -> Optional[ScoredDataGroup]:
        scores = ScoredDataGroup()
        scores["tokens"] = []
        scores["masks"] = []
        scores["scores"] = []
        scores["images"] = []
        random.shuffle(rollout_group_data)
        for item in rollout_group_data:
            out = tokenize_for_trainer(self.tokenizer, item[0])
            tokens = out["tokens"]
            masks = out["masks"]

            try:
                reply = item[0][-1]["content"]
                m = re.search(r"<answer>\s*(.*?)\s*</answer>", reply, re.IGNORECASE)
                model_answer = m.group(1).strip() if m else reply.strip()

                gold = item[1]
                g = re.search(r"<answer>\s*(.*?)\s*</answer>", gold, re.IGNORECASE)
                gold_answer = g.group(1).strip() if g else gold.strip()

                reward = model_answer.lower() == gold_answer.lower()
            except Exception:
                reward = False

            if len([i for i in masks if i != -100]) < 10:
                continue

            scores["tokens"].append(tokens)
            scores["masks"].append(masks)
            scores["scores"].append(1.0 if reward else -1.0)
            try:
                scores["images"].append(item[2])
            except Exception:
                scores["images"].append(None)

            if len(scores["tokens"]) >= self.config.group_size:
                break

        return scores

    @classmethod
    def config_init(cls) -> Tuple[BaseEnvConfig, List[OpenaiConfig]]:
        if not os.environ.get("OPENAI_API_KEY"):
            print("ERROR: OPENAI_API_KEY environment variable is not set!")
            sys.exit(1)

        config = BaseEnvConfig(
            wandb_name="pixmo_count",
            tokenizer_name="gpt2",
            group_size=2,
            use_wandb=False,
            max_num_workers=2,
            rollout_server_url="http://localhost:8000",
            total_steps=1000,
            batch_size=1,
            steps_per_eval=10,
            ensure_scores_are_not_same=False,
        )

        server_configs = [
            OpenaiConfig(
                model_name="gpt-4o",
                base_url=None,
                api_key=os.environ.get("OPENAI_API_KEY"),
                num_requests_for_eval=1,
            ),
        ]

        return config, server_configs


if __name__ == "__main__":
    PixmoCountEnv.cli()
