from sqlmodel import SQLModel 

from .user import User, UserPreferredCategories, UserPreferredNewsletter
from .news import Category, Press, RSS_URI, NewsRaw, NewsLetter, NewsLetterCategories
from .batch import NewsLettersCategory, NewsLetterTodayBatch
from .log import UserNewsLetterCTRLog
from .job import JobRun
