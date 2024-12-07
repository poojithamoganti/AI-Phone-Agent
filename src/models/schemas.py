from typing import List
from pydantic import BaseModel



# To Define correct structure for each question in the config file
class QuestionModel(BaseModel):
    topic: str
    question: str

# To Define correct structure to the whole config file to make sure it contains system prompt and a list of questions
class ConfigModel(BaseModel):
    system: str
    questions: List[QuestionModel]
