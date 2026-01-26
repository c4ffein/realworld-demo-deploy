#!/usr/bin/env python3
"""
RealWorld API Implementation - Single File Demo Server

⚠️  THIS IS PROBABLY NOT THE PROJECT YOU ARE LOOKING FOR  ⚠️

This is the opposite of what you'd expect in a real-world project: a single-file,
in-memory, framework-free implementation of the RealWorld API specification.

## Purpose
Demo backend for testing/development that manages all data in memory.
For a regular Python implementation that follows commonly accepted best practices, you can check:
https://github.com/c4ffein/realworld-django-ninja/

## Key Design Decisions
- **In-memory storage**: Data persists only during server runtime
  - Data can actually be saved on SIGINT reception if the DATA_FILE_PATH var env is set (so only handles `kill -2` rn)
- **Session isolation via token**: Each JWT token is bound to a session; re-login with credentials finds your data
- **Zero dependencies**: Python standard library only
- **Single file**: Entire server implementation in one module
- **Simple logging**: Of most operations (see `Deploy`)

## Rate Limiting
- Applied per IP address (IPv4) or /64 range (IPv6) via MAX_SESSIONS_PER_IP
- There are limits on the objects that will be saved in memory

## Deploy
- You should also rate limit per IPv4 address and IPv6 range through a reverse proxy
- You should still limit the max body size per request through a reverse proxy
- You should set LOG_LEVEL / LOG_FILE / LOG_MAX_SIZE / LOG_BACKUP_COUNT to enable rotating logs
- You should set CLIENT_IP_HEADER when running behind a reverse proxy (e.g., "X-Forwarded-For" or "X-Real-IP")
  - This ensures proper client IP detection for rate limiting when behind nginx, Apache, or load balancers
  - Without this setting, all requests will appear to come from the proxy's IP address
- `POPULATE_DEMO_DATA=True` allows you to copy demo data for each new session, including the default one

## Development Notes
- Vibe coded with Claude Code
- Tested against the regular test suites
- Usable as a demo backend, if risking to lose data is an acceptable tradeoff
- Working on this project was refreshing because the implementation approach differed a lot from typical web dev:
  It allowed for design decisions based on different constraints than these of more standard web projects

⚠️  DO NOT BASE NON-DEMO PROJECTS ON THIS SPECIFIC IMPLEMENTATION  ⚠️
"""

import hashlib
import json
import logging
import logging.handlers
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs
from os import getenv
from pathlib import Path
from time import time_ns
from traceback import format_tb
from typing import Dict, List, Optional, Tuple
from unittest import TestCase
from unittest.mock import patch

sys.path.insert(0, "./waitress/src")
from waitress import serve

#### CONFIGURATION #####################################################################################################


# url prefix
PATH_PREFIX = getenv("PATH_PREFIX", "")
PATH_PREFIX_PARTS = PATH_PREFIX.strip("/").split("/")
# security
DISABLE_ISOLATION_MODE = getenv("DISABLE_ISOLATION_MODE", "FALSE").lower() == "true"
MAX_SESSIONS = int(getenv("MAX_SESSIONS") or 3000)
MAX_SESSIONS_PER_IP = int(getenv("MAX_SESSIONS_PER_IP") or 30)
# client ip detection
CLIENT_IP_HEADER = getenv("CLIENT_IP_HEADER")  # Optional header name for client IP detection
# logging
LOG_LEVEL = getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE = getenv("LOG_FILE")  # Optional file logging
LOG_MAX_SIZE = int(getenv("LOG_MAX_SIZE", 10 * 1024 * 1024))  # 10MB default
LOG_BACKUP_COUNT = int(getenv("LOG_BACKUP_COUNT", 5))
# data persistence
DATA_FILE_PATH = (lambda path: Path(path) if path else None)(getenv("DATA_FILE_PATH"))
# max lengths for keys and objects per session
MAX_ID_LEN = int(getenv("MAX_ID_LEN", 64))
MAX_USERS_PER_SESSION = int(getenv("MAX_USERS_PER_SESSION", 60))
MAX_ARTICLES_PER_SESSION = int(getenv("MAX_ARTICLES_PER_SESSION", 20))
MAX_COMMENTS_PER_SESSION = int(getenv("MAX_COMMENTS_PER_SESSION", 20))
MAX_FOLLOWS_PER_SESSION = int(getenv("MAX_FOLLOWS_PER_SESSION", 100))
MAX_FAVORITES_PER_SESSION = int(getenv("MAX_FAVORITES_PER_SESSION", 100))
# max lengths for fields in models
MAX_LEN_USER_EMAIL = int(getenv("MAX_LEN_USER_EMAIL", 100))
MAX_LEN_USER_USERNAME = int(getenv("MAX_LEN_USER_USERNAME", 60))
MAX_LEN_USER_PASSWORD = int(getenv("MAX_LEN_USER_PASSWORD", 60))
MAX_LEN_USER_BIO = int(getenv("MAX_LEN_USER_BIO", 400))
MAX_LEN_USER_IMAGE = int(getenv("MAX_LEN_USER_IMAGE", 200))
MAX_LEN_ARTICLE_TITLE = int(getenv("MAX_LEN_ARTICLE_TITLE", 100))
MAX_LEN_ARTICLE_DESCRIPTION = int(getenv("MAX_LEN_ARTICLE_DESCRIPTION", 300))
MAX_LEN_ARTICLE_BODY = int(getenv("MAX_LEN_ARTICLE_BODY", 10000))
MAX_LEN_ARTICLE_TAG_LIST = int(getenv("MAX_LEN_ARTICLE_TAG_LIST", 10))
MAX_LEN_ARTICLE_TAG_LEN = int(getenv("MAX_LEN_ARTICLE_TAG_LEN", 20))
MAX_LEN_COMMENT_BODY = int(getenv("MAX_LEN_COMMENT_BODY", 3000))
# computed naive total size - not counting the links like favorites and followed
NAIVE_SIZE_USER = MAX_LEN_USER_EMAIL + MAX_LEN_USER_USERNAME + MAX_LEN_USER_PASSWORD + MAX_LEN_USER_BIO
NAIVE_SIZE_USER += MAX_LEN_USER_IMAGE
NAIVE_SIZE_TAG = MAX_LEN_ARTICLE_TAG_LIST + MAX_LEN_ARTICLE_TAG_LEN
NAIVE_SIZE_ARTICLE = MAX_LEN_ARTICLE_TITLE + MAX_LEN_ARTICLE_DESCRIPTION + MAX_LEN_ARTICLE_BODY + NAIVE_SIZE_TAG
NAIVE_SIZE_COMMENT = MAX_LEN_COMMENT_BODY
NAIVE_SIZE_SESSION_USER = NAIVE_SIZE_USER * MAX_USERS_PER_SESSION
NAIVE_SIZE_SESSION_ARTICLE = NAIVE_SIZE_ARTICLE * MAX_ARTICLES_PER_SESSION
NAIVE_SIZE_SESSION_COMMENT = NAIVE_SIZE_COMMENT * MAX_COMMENTS_PER_SESSION
NAIVE_SIZE_SESSION = NAIVE_SIZE_SESSION_USER + NAIVE_SIZE_SESSION_ARTICLE + NAIVE_SIZE_SESSION_COMMENT
NAIVE_SIZE_TOTAL = NAIVE_SIZE_SESSION * MAX_SESSIONS
# populate demo data
POPULATE_DEMO_DATA = getenv("POPULATE_DEMO_DATA", "FALSE").lower() == "true"
DEMO_DATA_DEFAULT_IMAGE = getenv(
    "DEMO_DATA_DEFAULT_IMAGE",
    "https://upload.wikimedia.org/wikipedia/commons/thumb/8/8f/Cute-kittens-12929201-1600-1200.jpg"
    "/960px-Cute-kittens-12929201-1600-1200.jpg",
)


#### LOGGING ###########################################################################################################


class JSONFormatter(logging.Formatter):
    def format(self, record):
        log_entry = {
            "timestamp": datetime.fromtimestamp(record.created).strftime("%Y-%m-%dT%H:%M:%S.%f"),
            "logger": record.name,
            "level": record.levelname,
            "category": (lambda v: f"{record.name}.{v}" if v is not None else record.name)(
                getattr(record, "category", None)
            ),
            "message": record.getMessage(),
            "data": getattr(record, "data", {}),
        }
        return json.dumps(log_entry)


def setup_logging():
    """Set up logging configuration with console and optional file output"""
    # Create root logger
    logger = logging.getLogger()
    logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    # Clear any existing handlers
    logger.handlers.clear()
    # Create formatter
    formatter = JSONFormatter()
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    # Optional file handler with rotation
    if LOG_FILE:
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=LOG_MAX_SIZE, backupCount=LOG_BACKUP_COUNT
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    return logger


# Set up logging
main_logger = setup_logging()
# Create category-specific loggers
auth_logger = logging.getLogger("auth")
http_logger = logging.getLogger("http")
storage_logger = logging.getLogger("storage")
session_management_logger = logging.getLogger("storage.session_management")
security_logger = logging.getLogger("security")
config_logger = logging.getLogger("config")
lifecycle_logger = logging.getLogger("lifecycle")


def log_structured(logger, level, message, category=None, **extra_data_fields):
    """Helper function to log structured data as JSON"""
    logger.log(level, message, extra={"category": category or "general", "data": extra_data_fields})


#### HELPERS ###########################################################################################################


def format_datetime(dt: datetime) -> str:
    """Format datetime to ISO 8601"""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def get_current_time() -> str:
    """Get current time in ISO format"""
    return format_datetime(datetime.now(timezone.utc))


def hash_password(password: str) -> str:
    """Simple password hashing"""
    return hashlib.sha256(password.encode()).hexdigest()


#### DEMO_DATA #########################################################################################################


def populate_demo_data(storage: "InMemoryStorage"):
    """Populate InMemoryStorage with demo data for testing/demo purposes"""
    current_time = get_current_time()
    # Create demo users
    users_data = [
        {
            "email": "john.doe@example.com",
            "username": "johndoe",
            "password": hash_password("password123"),
            "bio": "Full-stack developer passionate about clean code and innovative solutions. Love working with modern web technologies.",
            "image": DEMO_DATA_DEFAULT_IMAGE,
            "createdAt": current_time,
        },
        {
            "email": "jane.smith@example.com",
            "username": "janesmith",
            "password": hash_password("password123"),
            "bio": "Frontend developer with a keen eye for UI/UX design. Specializing in React and modern CSS frameworks.",
            "image": DEMO_DATA_DEFAULT_IMAGE,
            "createdAt": current_time,
        },
        {
            "email": "mike.wilson@example.com",
            "username": "mikewilson",
            "password": hash_password("password123"),
            "bio": "Backend engineer focused on scalable architecture and DevOps. Enthusiast of cloud technologies and automation.",
            "image": DEMO_DATA_DEFAULT_IMAGE,
            "createdAt": current_time,
        },
        {
            "email": "sarah.chen@example.com",
            "username": "sarahchen",
            "password": hash_password("password123"),
            "bio": "Data scientist and machine learning engineer. Passionate about turning data into actionable insights.",
            "image": DEMO_DATA_DEFAULT_IMAGE,
            "createdAt": current_time,
        },
    ]
    # Add users and store their IDs
    user_ids = []
    for user_data in users_data:
        user = storage.users.add(user_data)
        user_ids.append(user["id"])
    # Create demo articles
    articles_data = [
        {
            "slug": "how-to-learn-javascript-efficiently",
            "title": "How to Learn JavaScript Efficiently",
            "description": "A comprehensive guide to mastering JavaScript from beginner to advanced level",
            "body": "Learning JavaScript can be overwhelming with so many resources available. Here's a structured approach that has helped thousands of developers master this essential language.\n\n## Start with the Fundamentals\n\nBefore diving into frameworks, master the core concepts: variables, functions, objects, and arrays. Understanding these building blocks is crucial for writing clean, maintainable code.\n\n## Practice with Real Projects\n\nThe best way to learn is by building actual applications. Start with simple projects like a todo list or calculator, then gradually increase complexity.\n\n## Join the Community\n\nEngage with other developers through forums, Discord servers, and local meetups. The JavaScript community is incredibly welcoming and helpful.",
            "tagList": ["javascript", "programming", "webdev", "beginners"],
            "author_id": user_ids[0],
            "createdAt": current_time,
            "updatedAt": current_time,
        },
        {
            "slug": "react-hooks-best-practices",
            "title": "React Hooks: Best Practices and Common Pitfalls",
            "description": "Essential patterns and anti-patterns when working with React Hooks",
            "body": "React Hooks have revolutionized how we write React components, but they come with their own set of best practices and potential pitfalls.\n\n## useEffect Dependencies\n\nOne of the most common mistakes is forgetting to include dependencies in the useEffect array. This can lead to stale closures and unexpected behavior.\n\n## Custom Hooks for Reusability\n\nCreate custom hooks to encapsulate stateful logic that can be shared across components. This promotes code reuse and maintainability.\n\n## Performance Considerations\n\nUse useMemo and useCallback judiciously. Don't optimize prematurely, but be aware of when these hooks can help prevent unnecessary re-renders.",
            "tagList": ["react", "hooks", "javascript", "frontend"],
            "author_id": user_ids[1],
            "createdAt": current_time,
            "updatedAt": current_time,
        },
        {
            "slug": "building-scalable-apis-with-node-js",
            "title": "Building Scalable APIs with Node.js",
            "description": "Architectural patterns and best practices for creating robust backend services",
            "body": "Building scalable APIs requires careful consideration of architecture, error handling, and performance optimization.\n\n## API Design Principles\n\nFollow RESTful conventions and use appropriate HTTP status codes. Design your API to be intuitive and self-documenting.\n\n## Error Handling Strategy\n\nImplement comprehensive error handling with proper logging and monitoring. Use middleware to handle errors consistently across your application.\n\n## Database Optimization\n\nOptimize database queries and consider implementing caching strategies for frequently accessed data. Connection pooling is essential for production applications.",
            "tagList": ["nodejs", "api", "backend", "architecture"],
            "author_id": user_ids[2],
            "createdAt": current_time,
            "updatedAt": current_time,
        },
        {
            "slug": "introduction-to-machine-learning-for-developers",
            "title": "Introduction to Machine Learning for Developers",
            "description": "Getting started with ML concepts and practical applications for software developers",
            "body": "Machine learning might seem intimidating, but it's more accessible than ever for developers looking to expand their skillset.\n\n## Understanding the Basics\n\nStart with supervised learning concepts like classification and regression. These form the foundation for more complex ML algorithms.\n\n## Practical Tools and Libraries\n\nPython's scikit-learn is perfect for beginners, while TensorFlow and PyTorch offer more advanced capabilities for deep learning projects.\n\n## Data Preprocessing\n\nMost of ML work involves cleaning and preparing data. Learn to handle missing values, normalize features, and split datasets properly.",
            "tagList": ["machinelearning", "python", "ai", "datascience"],
            "author_id": user_ids[3],
            "createdAt": current_time,
            "updatedAt": current_time,
        },
    ]
    # Add articles and store their IDs
    article_ids = []
    for article_data in articles_data:
        article = storage.articles.add(article_data)
        article_ids.append(article["id"])
    # Create demo comments
    comments_data = [
        {
            "body": "Great article! I've been struggling with JavaScript concepts and this really helps clarify things.",
            "article_id": article_ids[0],
            "author_id": user_ids[1],
            "createdAt": current_time,
            "updatedAt": current_time,
        },
        {
            "body": "The project-based approach really works. I built three projects following this guide and learned so much!",
            "article_id": article_ids[0],
            "author_id": user_ids[2],
            "createdAt": current_time,
            "updatedAt": current_time,
        },
        {
            "body": "useEffect dependencies caught me so many times when I was learning React. Wish I had read this earlier!",
            "article_id": article_ids[1],
            "author_id": user_ids[0],
            "createdAt": current_time,
            "updatedAt": current_time,
        },
        {
            "body": "Custom hooks are a game-changer. They make components so much cleaner and more reusable.",
            "article_id": article_ids[1],
            "author_id": user_ids[3],
            "createdAt": current_time,
            "updatedAt": current_time,
        },
        {
            "body": "Error handling is definitely something I need to improve on. Thanks for the practical tips!",
            "article_id": article_ids[2],
            "author_id": user_ids[1],
            "createdAt": current_time,
            "updatedAt": current_time,
        },
        {
            "body": "Connection pooling made such a difference in my API performance. Great advice!",
            "article_id": article_ids[2],
            "author_id": user_ids[0],
            "createdAt": current_time,
            "updatedAt": current_time,
        },
        {
            "body": "As someone new to ML, this is exactly the kind of practical introduction I was looking for.",
            "article_id": article_ids[3],
            "author_id": user_ids[1],
            "createdAt": current_time,
            "updatedAt": current_time,
        },
        {
            "body": "The data preprocessing section is spot on. It's definitely where most of the work happens in ML projects.",
            "article_id": article_ids[3],
            "author_id": user_ids[2],
            "createdAt": current_time,
            "updatedAt": current_time,
        },
    ]
    # Add comments
    for comment_data in comments_data:
        storage.comments.add(comment_data)
    # Create some follow relationships
    # John follows Jane and Mike
    storage.follows.add(user_ids[0], user_ids[1])
    storage.follows.add(user_ids[0], user_ids[2])
    # Jane follows Sarah
    storage.follows.add(user_ids[1], user_ids[3])
    # Mike follows John and Sarah
    storage.follows.add(user_ids[2], user_ids[0])
    storage.follows.add(user_ids[2], user_ids[3])
    # Create some favorite relationships
    # John favorites Jane's and Sarah's articles
    storage.favorites.add(user_ids[0], article_ids[1])
    storage.favorites.add(user_ids[0], article_ids[3])
    # Jane favorites Mike's article
    storage.favorites.add(user_ids[1], article_ids[2])
    # Mike favorites John's article
    storage.favorites.add(user_ids[2], article_ids[0])
    # Sarah favorites John's and Jane's articles
    storage.favorites.add(user_ids[3], article_ids[0])
    storage.favorites.add(user_ids[3], article_ids[1])


#### IMPLEMENTATION ####################################################################################################


def normalize_id(value):
    if type(value) is int:
        value = str(value)
    if type(value) is str:
        if len(value) > MAX_ID_LEN:
            raise ValueError("id is too long")
        return value
    raise ValueError("id must be an int or an str")


class InMemoryModel:
    """
    when rolling with new ids, we may safe-delete so we don't break any link in the storage (maybe through a callback)
    won't implement for now as the ROI isn't really there
    """

    def __init__(self, max_count):
        self.max_count: int = max_count
        self.objects: Dict[str, object] = {}
        self.last_accessed_ids: List[str] = []  # perf ok because of the objects per model per session limit
        self.current_id_counter = 1
        if self.max_count <= 0:
            raise ValueError("invalid value for max_count")

    def add(self, obj):
        if len(str(self.current_id_counter)) > MAX_ID_LEN:
            raise ValueError("cannot allocate id: we reached MAX_ID_LEN limit")
        self.objects[str(self.current_id_counter)] = obj
        obj["id"] = str(self.current_id_counter)
        self.current_id_counter += 1
        if len(self.objects) > self.max_count:
            evicted_id = self.last_accessed_ids[0]
            log_structured(
                security_logger,
                logging.WARNING,
                "Rate limit reached - Object storage full, evicting oldest object",
                rate_limit_type="object_storage",
                max_count=self.max_count,
                evicted_id=evicted_id,
                new_id=obj["id"],
            )
            del self.objects[evicted_id]
            self.last_accessed_ids = self.last_accessed_ids[1:] + [obj["id"]]
        else:
            self.last_accessed_ids.append(obj["id"])
        log_structured(
            storage_logger,
            logging.DEBUG,
            "object added",
            operation="add",
            object_id=obj["id"],
            total_objects=len(self.objects),
        )
        return obj

    def get(self, _id):
        _id = normalize_id(_id)
        if _id not in self.objects:
            log_structured(
                storage_logger, logging.DEBUG, "get - object not found", operation="get", object_id=_id, found=False
            )
            return None
        self.last_accessed_ids = [*(e for e in self.last_accessed_ids if e != _id), _id]
        log_structured(
            storage_logger, logging.DEBUG, "get - object retrieved", operation="get", object_id=_id, found=True
        )
        return self.objects[_id]

    def keys(self):
        return self.objects.keys()

    def values(self):
        return self.objects.values()

    def items(self):
        return self.objects.items()

    def delete(self, _id):
        _id = normalize_id(_id)
        if _id in self.objects:
            del self.objects[_id]
            self.last_accessed_ids = [cid for cid in self.last_accessed_ids if cid != _id]
            log_structured(
                storage_logger,
                logging.DEBUG,
                "delete - object deleted",
                operation="delete",
                object_id=_id,
                success=True,
            )
            return True
        log_structured(
            storage_logger,
            logging.DEBUG,
            "delete - object not deleted",
            operation="delete",
            object_id=_id,
            success=False,
        )
        return False


class InMemoryLinks:
    def __init__(self, max_count):
        self.max_count: int = max_count
        self.links: List[Tuple[int, int]] = []  # cheaper implem to limit global number of links and wipe oldest

    def add(self, source, target):
        source, target = normalize_id(source), normalize_id(target)
        if self.max_count == 0:
            return
        _index = self.links.index((source, target)) if (source, target) in self.links else None
        if _index is not None:
            self.links = [*self.links[:_index], *self.links[_index + 1 :], (source, target)]
        elif len(self.links) >= self.max_count:
            evicted_link = self.links[0] if self.links else None
            log_structured(
                security_logger,
                logging.WARNING,
                "Rate limit reached - Link storage full, evicting oldest link",
                rate_limit_type="link_storage",
                max_count=self.max_count,
                evicted_link=evicted_link,
                new_link=(source, target),
            )
            self.links = [*self.links[1:], (source, target)]
        else:
            self.links = [*self.links, (source, target)]

    def remove(self, source, target):
        source, target = normalize_id(source), normalize_id(target)
        _index = self.links.index((source, target)) if (source, target) in self.links else None
        if _index is not None:
            self.links = [*self.links[:_index], *self.links[_index + 1 :]]

    def is_linked(self, source, target):
        source, target = normalize_id(source), normalize_id(target)
        return (source, target) in self.links

    def targets_for_source(self, wanted_source):
        return [target for source, target in self.links if source == normalize_id(wanted_source)]

    def sources_for_target(self, wanted_target):
        return [source for source, target in self.links if target == normalize_id(wanted_target)]

    def delete_source(self, source_to_delete):
        self.links = [(source, target) for source, target in self.links if source != normalize_id(source_to_delete)]

    def delete_target(self, target_to_delete):
        self.links = [(source, target) for source, target in self.links if target != normalize_id(target_to_delete)]


class InMemoryStorage:
    """In-memory storage for all data"""

    def __init__(self):
        self.users = InMemoryModel(max_count=MAX_USERS_PER_SESSION)
        self.articles = InMemoryModel(max_count=MAX_ARTICLES_PER_SESSION)
        self.comments = InMemoryModel(max_count=MAX_COMMENTS_PER_SESSION)
        self.follows = InMemoryLinks(max_count=MAX_FOLLOWS_PER_SESSION)  # user_id -> followed user_ids
        self.favorites = InMemoryLinks(max_count=MAX_FAVORITES_PER_SESSION)  # user_id -> favorited article_ids
        if POPULATE_DEMO_DATA:
            populate_demo_data(self)


class _StorageContainer:
    """
    Removes storage for the least used session once max_sessions is reached
    Limits the number of sessions the same IPv4 address or IPv6 range can handle
    It is arguable that this class breaks separation of concerns
    But the intricate relationships between the multiple data structures needed to achieve the target behavior
    make this implementation an acceptable choice
    """

    # init

    def __init__(self, disable_isolation_mode=DISABLE_ISOLATION_MODE, max_sessions=MAX_SESSIONS):
        self.DISABLE_ISOLATION_MODE = disable_isolation_mode
        self.MAX_SESSIONS = max_sessions
        self.heap = []  # list of (priority, obj_id, data, index, client_ip)
        self.index_map = {}  # session_id -> heap index
        self.jwt_to_session = {}  # jwt_token -> session -- it's a bijective relation; maybe multiple sessions -> data
        self.jwt_to_session_order = []  # list of jwt_token
        self.ip_to_sessions = {}  # ip -> list of session_ids
        if not MAX_SESSIONS_PER_IP or MAX_SESSIONS_PER_IP < 1:
            raise ValueError(f"MAX_SESSIONS_PER_IP is set to {MAX_SESSIONS_PER_IP}, you need at least one")

    # heap + index_map operations -> call _handle_client_ip_and_session helpers as side-effect

    def _push(self, priority, obj_id, data=None, client_ip=None):
        """Push a session onto the heap, add it to the index"""
        index = len(self.heap)
        item = [priority, obj_id, data, index, client_ip]  # includes index and client_ip
        self.heap.append(item)
        self.index_map[obj_id] = index
        self._sift_up(index)

    def _pop(self):
        """Pop the oldest session from the heap, clean it form the index"""
        if not self.heap:
            return None
        # Remove from index map
        root_item = self.heap[0]
        del self.index_map[root_item[1]]
        if len(self.heap) == 1:
            return self.heap.pop()
        # Move last item to root and sift down (updates indexes)
        last_item = self.heap.pop()
        self.heap[0] = last_item
        self.heap[0][3] = 0  # Update index
        self.index_map[last_item[1]] = 0
        self._sift_down(0)
        return root_item

    def _update_priority(self, obj_id, new_priority):
        """Update the priority of an existing item"""
        if obj_id not in self.index_map:
            raise ValueError(f"Object {obj_id} not found in heap")
        index = self.index_map[obj_id]
        old_priority = self.heap[index][0]
        self.heap[index][0] = new_priority
        if new_priority < old_priority:
            self._sift_up(index)
        elif new_priority > old_priority:
            self._sift_down(index)

    def _sift_up(self, index):
        """Restore heap property upward"""
        while index > 0:
            parent_index = (index - 1) // 2
            if self.heap[index][0] >= self.heap[parent_index][0]:
                break
            # Swap items
            self._swap(index, parent_index)
            index = parent_index

    def _sift_down(self, index):
        """Restore heap property downward"""
        while True:
            smallest = index
            left_child = 2 * index + 1
            right_child = 2 * index + 2
            if left_child < len(self.heap) and self.heap[left_child][0] < self.heap[smallest][0]:
                smallest = left_child
            if right_child < len(self.heap) and self.heap[right_child][0] < self.heap[smallest][0]:
                smallest = right_child
            if smallest == index:
                break
            self._swap(index, smallest)
            index = smallest

    def _swap(self, i, j):
        """Swap two items and update their indices"""
        self.index_map[self.heap[i][1]], self.index_map[self.heap[j][1]] = j, i  # update index map
        self.heap[i][3], self.heap[j][3] = j, i  # update indices in items
        self.heap[i], self.heap[j] = self.heap[j], self.heap[i]  # swap items

    # _handle_client_ip_and_session helpers -> actualize ip and session relations

    def _normalize_ip_for_limiting(self, ip):
        """Normalize IP for session limiting - IPv4 as-is, IPv6 to /64 range"""
        if ip.endswith("/64"):  # makes it safe to call multiple times
            return ip
        if ":" in ip:  # IPv6, limit per /64 subnet (first 4 groups)
            parts = ip.split(":")
            return ":".join(parts[:4]) + "::/64" if len(parts) >= 4 else ip + "/64"  # unsafe but shouldn't happen
        return ip  # IPv4 as-is

    def _handle_client_ip_and_session_eviction(self, identifier, client_ip):
        """Helper that cleanly removes a session from ip_to_sessions: removes the ip entirely if it becomes empty"""
        if not client_ip:
            log_structured(
                session_management_logger,
                logging.DEBUG,
                "Client IP and session eviction skipped",
                identifier=identifier,
                client_ip=None,
            )
            return
        normalized_ip = self._normalize_ip_for_limiting(client_ip)
        if normalized_ip in self.ip_to_sessions:
            sessions_before = len(self.ip_to_sessions[normalized_ip])
            self.ip_to_sessions[normalized_ip] = [e for e in self.ip_to_sessions[normalized_ip] if e != identifier]
            if not self.ip_to_sessions[normalized_ip]:  # Remove empty lists
                del self.ip_to_sessions[normalized_ip]
                log_structured(
                    session_management_logger,
                    logging.DEBUG,
                    "Client IP and session eviction completed - IP entry removed",
                    identifier=identifier,
                    client_ip=client_ip,
                    normalized_ip=normalized_ip,
                    sessions_before=sessions_before,
                )
            else:
                log_structured(
                    session_management_logger,
                    logging.DEBUG,
                    "Client IP and session eviction completed - session removed",
                    identifier=identifier,
                    client_ip=client_ip,
                    normalized_ip=normalized_ip,
                    sessions_before=sessions_before,
                    sessions_after=len(self.ip_to_sessions[normalized_ip]),
                )
        else:
            log_structured(
                session_management_logger,
                logging.DEBUG,
                "Client IP and session eviction - IP not found",
                identifier=identifier,
                client_ip=client_ip,
                normalized_ip=normalized_ip,
            )

    def _handle_client_ip_and_session_addition(self, identifier, client_ip):
        """Helper that adds a session to ip_to_sessions: may remove a session as a side_effect"""
        if not client_ip:
            log_structured(
                session_management_logger,
                logging.DEBUG,
                "Client IP and session addition skipped - no client IP",
                identifier=identifier,
            )
            return
        normalized_ip = self._normalize_ip_for_limiting(client_ip)
        if normalized_ip not in self.ip_to_sessions:
            self.ip_to_sessions[normalized_ip] = [identifier]
            log_structured(
                session_management_logger,
                logging.DEBUG,
                "Client IP and session addition completed - new IP entry",
                identifier=identifier,
                client_ip=client_ip,
                normalized_ip=normalized_ip,
            )
            return
        sessions_before = len(self.ip_to_sessions[normalized_ip])
        self.ip_to_sessions[normalized_ip].append(identifier)
        sessions_removed = 0
        while len(self.ip_to_sessions[normalized_ip]) > MAX_SESSIONS_PER_IP:
            session_id_to_remove = self.ip_to_sessions[normalized_ip][0]
            self._update_priority(session_id_to_remove, 0)
            self._pop()
            self.ip_to_sessions[normalized_ip] = self.ip_to_sessions[normalized_ip][1:]
            sessions_removed += 1
        log_structured(
            session_management_logger,
            logging.DEBUG,
            "Client IP and session addition completed",
            identifier=identifier,
            client_ip=client_ip,
            normalized_ip=normalized_ip,
            sessions_before=sessions_before,
            sessions_after=len(self.ip_to_sessions[normalized_ip]),
            sessions_removed=sessions_removed,
        )

    def _handle_client_ip_and_session_priority(self, session_id, client_ip):
        """
        Helper that modifies ip_to_sessions accordingly to the changes to the session priorities
        May actually pop the oldest session for an ip if we reattribute a session from an ip to another
        """
        if not client_ip:
            log_structured(
                session_management_logger,
                logging.DEBUG,
                "Client IP and session priority skipped - no client IP",
                session_id=session_id,
            )
            return
        normalized_ip = self._normalize_ip_for_limiting(client_ip)
        _, _, _, _, saved_client_ip = self.heap[self.index_map[session_id]]
        if saved_client_ip == normalized_ip:
            if normalized_ip not in self.ip_to_sessions:  # shouldn't happen but safer to handle it anyway
                self.ip_to_sessions[normalized_ip] = [session_id]
                log_structured(
                    session_management_logger,
                    logging.DEBUG,
                    "Client IP and session priority - created missing IP entry",
                    session_id=session_id,
                    client_ip=client_ip,
                    normalized_ip=normalized_ip,
                )
                return
            sessions_before = len(self.ip_to_sessions[normalized_ip])
            self.ip_to_sessions[normalized_ip] = [
                e
                for e in self.ip_to_sessions[normalized_ip]
                if e != session_id  # still safe if not present
            ] + [session_id]  # we should never exceed MAX_SESSIONS_PER_IP as this session is supposed to be here though
            log_structured(
                session_management_logger,
                logging.DEBUG,
                "Client IP and session priority - session moved to end",
                session_id=session_id,
                client_ip=client_ip,
                normalized_ip=normalized_ip,
                sessions_count=sessions_before,
            )
            return
        log_structured(
            session_management_logger,
            logging.DEBUG,
            "Client IP and session priority - triggering reattribution",
            session_id=session_id,
            client_ip=client_ip,
            normalized_ip=normalized_ip,
            saved_client_ip=saved_client_ip,
        )
        self._handle_client_ip_and_session_reattribution(session_id, saved_client_ip, normalized_ip)

    def _handle_client_ip_and_session_reattribution(self, session_id, normalized_saved_ip, normalized_client_ip):
        """expects normalized_client_ip and normalized_saved_ip to both be defined, and different"""
        log_structured(
            session_management_logger,
            logging.DEBUG,
            "Client IP and session reattribution started",
            session_id=session_id,
            normalized_saved_ip=normalized_saved_ip,
            normalized_client_ip=normalized_client_ip,
        )
        self.heap[self.index_map[session_id]][4] = normalized_client_ip  # update the client_ip in the data struct
        saved_ip_removed = False
        saved_ip_sessions_before, saved_ip_sessions_after = None, None
        if normalized_saved_ip and normalized_saved_ip in self.ip_to_sessions:
            saved_ip_sessions_before = len(self.ip_to_sessions[normalized_saved_ip])
            self.ip_to_sessions[normalized_saved_ip] = [
                e for e in self.ip_to_sessions[normalized_saved_ip] if e != session_id
            ]
            saved_ip_sessions_after = len(self.ip_to_sessions[normalized_saved_ip])
            if not self.ip_to_sessions[normalized_saved_ip]:  # Remove empty lists
                del self.ip_to_sessions[normalized_saved_ip]
                saved_ip_removed = True
        client_ip_sessions_before = len(self.ip_to_sessions.get(normalized_client_ip, []))
        self.ip_to_sessions[normalized_client_ip] = [
            e
            for e in self.ip_to_sessions.get(normalized_client_ip, [])
            if e != session_id  # still safe if not present
        ] + [session_id]
        sessions_removed = 0
        while len(self.ip_to_sessions[normalized_client_ip]) > MAX_SESSIONS_PER_IP:
            self._update_priority(self.ip_to_sessions[normalized_client_ip][0], 0)
            self._pop()
            self.ip_to_sessions[normalized_client_ip] = self.ip_to_sessions[normalized_client_ip][1:]
            sessions_removed += 1
        log_structured(
            session_management_logger,
            logging.DEBUG,
            "Client IP and session reattribution completed",
            session_id=session_id,
            normalized_saved_ip=normalized_saved_ip,
            normalized_client_ip=normalized_client_ip,
            saved_ip_removed=saved_ip_removed,
            saved_ip_sessions_before=saved_ip_sessions_before,
            saved_ip_sessions_after=saved_ip_sessions_after,
            client_ip_sessions_before=client_ip_sessions_before,
            client_ip_sessions_after=len(self.ip_to_sessions[normalized_client_ip]),
            sessions_removed=sessions_removed,
        )

    # only external method that should get called

    def push(self, priority, session_id, data=None, client_ip=None):
        """Push a session onto the heap, add it to the index, and manage the sessions by ip (possible evictment)"""
        self._push(priority, session_id, data, client_ip)
        self._handle_client_ip_and_session_addition(session_id, client_ip)  # can potentially remove a session

    def pop(self):
        """Pop the oldest session from the heap, clean it form the index, and removes it from the sessions by ip"""
        root_item = self._pop()
        if root_item is None:
            return None
        _, session_id, _, _, client_ip = root_item
        self._handle_client_ip_and_session_eviction(session_id, client_ip)  # remove the session
        return root_item

    def update_priority(self, obj_id, new_priority, client_ip=None):
        """Update the priority of an existing item, and may reattribute it to another ip if there is a discrepancy"""
        self._update_priority(obj_id, new_priority)
        self._handle_client_ip_and_session_priority(obj_id, client_ip)  # also manages session's ip != client_ip

    def get_storage(self, session_id_from_cookie, client_ip=None, jwt_token=None):
        if self.DISABLE_ISOLATION_MODE:
            if not self.heap:
                self.heap.append(InMemoryStorage())  # Not using expected implem
            return None, self.heap[0]  # Heap is not filled with the expected lists
        target_session_id = session_id_from_cookie
        if not target_session_id and jwt_token:
            session_id_from_token = self.jwt_to_session.get(jwt_token)
            if session_id_from_token and session_id_from_token in self.index_map:
                target_session_id = session_id_from_token
                self.jwt_to_session_order = [
                    *(e for e in self.jwt_to_session_order if e != target_session_id),
                    jwt_token,
                ]
        target_session_id = target_session_id or str(uuid.uuid4())
        storage_container_index = self.index_map.get(target_session_id)
        if storage_container_index is None:  # create the session, push and pop manage the ip
            if len(self.index_map) >= self.MAX_SESSIONS:
                evicted_session = self.pop()
                if evicted_session:
                    log_structured(
                        security_logger,
                        logging.INFO,
                        "Rate limit reached - Session storage full, evicting session",
                        rate_limit_type="session_storage",
                        max_sessions=self.MAX_SESSIONS,
                        evicted_session_id=evicted_session[1],
                        new_session_id=target_session_id,
                    )
            self.push(time_ns(), target_session_id, data=InMemoryStorage(), client_ip=client_ip)
            log_structured(
                security_logger,
                logging.INFO,
                "New session created",
                session_event="created",
                session_id=target_session_id,
                total_sessions=len(self.index_map),
                client_ip=client_ip,
            )
            return target_session_id, self.heap[self.index_map.get(target_session_id)][2]
        r = self.heap[storage_container_index][2]  # existing session
        self.update_priority(target_session_id, time_ns(), client_ip=client_ip)  # manage priority and ip/session
        log_structured(
            storage_logger,
            logging.DEBUG,
            "Session accessed",
            session_event="accessed",
            session_id_from_cookie=session_id_from_cookie,
            target_session_id=target_session_id,
        )
        return target_session_id, r

    def find_session_by_credentials(self, email, hashed_password):
        """
        Search all sessions for a user with matching email and password
        Returns (session_id, storage) if found, (None, None) otherwise
        This allows login to work across session eviction by finding the user's original session
        WARNING: Suboptimal, we may want to have a dict + heap mechanism to not reach O(n) with sessions, but goodenough
        """
        if self.DISABLE_ISOLATION_MODE:
            return None, None
        for heap_item in self.heap:
            _, session_id, storage, _, _ = heap_item
            # Search for user with matching email and password in this session's storage
            for user in storage.users.values():
                if user["email"] == email and user["password"] == hashed_password:
                    log_structured(
                        auth_logger,
                        logging.DEBUG,
                        "Found user in existing session",
                        email=email,
                        session_id=session_id,
                        user_id=user["id"],
                    )
                    return session_id, storage
        return None, None

    def bind_jwt_to_session_id(self, jwt_token, session_id):
        """
        Binds a JWT token to existing session storage
        This method must only be called after a storage has already been retrieved for that session_id
        This method is expected to only be called during the login and registration processes
        (hence why it is acceptable to let it be algorithmically inefficient)
        """
        if self.DISABLE_ISOLATION_MODE:
            return
        jwt_tokens_to_remove = {t for t, s in self.jwt_to_session.items() if t == jwt_token or s == session_id}
        if len(self.jwt_to_session_order) >= self.MAX_SESSIONS:
            jwt_tokens_to_remove.add(self.jwt_to_session_order[0])
        self.jwt_to_session_order = [t for t in self.jwt_to_session_order if t not in jwt_tokens_to_remove]
        for jwt_token_to_remove in jwt_tokens_to_remove:
            del self.jwt_to_session[jwt_token_to_remove]
        # Bind the JWT token to the session
        self.jwt_to_session[jwt_token] = session_id
        self.jwt_to_session_order.append(jwt_token)


storage_container = _StorageContainer()


def save_data():
    """Save storage_container data to JSON file - WARNING the current implementation wipes all session to ip data"""
    if not DATA_FILE_PATH:
        log_structured(
            storage_logger,
            logging.DEBUG,
            "Data persistence skipped - DATA_FILE_PATH not configured",
            data_file_path=None,
        )
        return False

    log_structured(
        storage_logger, logging.INFO, "Starting data save", data_file_path=str(DATA_FILE_PATH), operation="save_start"
    )

    data = {}
    session_count = 0
    # Save heap items in order from oldest to newest by popping from heap
    saved_items = []
    while storage_container.heap:
        heap_item = storage_container.pop()
        if heap_item:
            priority, session_id, storage, _, client_ip = heap_item
            saved_items.append((priority, session_id, storage, client_ip))
            session_count += 1
            session_data = {
                "users": {
                    "objects": dict(storage.users.objects),
                    "last_accessed_ids": storage.users.last_accessed_ids,
                    "current_id_counter": storage.users.current_id_counter,
                },
                "articles": {
                    "objects": dict(storage.articles.objects),
                    "last_accessed_ids": storage.articles.last_accessed_ids,
                    "current_id_counter": storage.articles.current_id_counter,
                },
                "comments": {
                    "objects": dict(storage.comments.objects),
                    "last_accessed_ids": storage.comments.last_accessed_ids,
                    "current_id_counter": storage.comments.current_id_counter,
                },
                "follows": storage.follows.links,
                "favorites": storage.favorites.links,
            }
            data[session_id] = session_data
    for priority, session_id, storage, client_ip in saved_items:
        storage_container.push(priority, session_id, storage, client_ip=client_ip)
    try:
        with DATA_FILE_PATH.open("w") as f:
            json.dump(data, f, indent=2)
        log_structured(
            storage_logger,
            logging.INFO,
            "Data saved successfully",
            data_file_path=str(DATA_FILE_PATH),
            session_count=session_count,
            operation="save_success",
        )
        return True
    except Exception as e:
        log_structured(
            storage_logger,
            logging.ERROR,
            "Error saving data",
            data_file_path=str(DATA_FILE_PATH),
            error=str(e),
            operation="save_error",
        )
    return False


def load_data():
    """Load storage_container data from JSON file"""
    if not DATA_FILE_PATH:
        log_structured(
            storage_logger,
            logging.DEBUG,
            "Data persistence skipped - DATA_FILE_PATH not configured",
            data_file_path=None,
        )
        return

    log_structured(
        storage_logger, logging.INFO, "Starting data load", data_file_path=str(DATA_FILE_PATH), operation="load_start"
    )

    try:
        with DATA_FILE_PATH.open("r") as f:
            data = json.load(f)
        session_count = 0
        for session_id, session_data in data.items():
            storage = InMemoryStorage()
            session_count += 1
            if "users" in session_data:
                storage.users.objects.update(session_data["users"].get("objects", {}))
                storage.users.last_accessed_ids = session_data["users"].get("last_accessed_ids", [])
                storage.users.current_id_counter = session_data["users"].get("current_id_counter", 1)
            if "articles" in session_data:
                storage.articles.objects.update(session_data["articles"].get("objects", {}))
                storage.articles.last_accessed_ids = session_data["articles"].get("last_accessed_ids", [])
                storage.articles.current_id_counter = session_data["articles"].get("current_id_counter", 1)
            if "comments" in session_data:
                storage.comments.objects.update(session_data["comments"].get("objects", {}))
                storage.comments.last_accessed_ids = session_data["comments"].get("last_accessed_ids", [])
                storage.comments.current_id_counter = session_data["comments"].get("current_id_counter", 1)
            storage.follows.links = session_data.get("follows", [])
            storage.favorites.links = session_data.get("favorites", [])
            storage_container._push(time_ns(), session_id, storage)
        DATA_FILE_PATH.unlink()  # ensures we won't reload past data
        log_structured(
            storage_logger,
            logging.INFO,
            "Data loaded successfully",
            data_file_path=str(DATA_FILE_PATH),
            session_count=session_count,
            operation="load_success",
        )
    except FileNotFoundError:
        log_structured(
            storage_logger,
            logging.INFO,
            "No data file found - starting with empty storage",
            data_file_path=str(DATA_FILE_PATH),
            operation="load_no_file",
        )
        pass
    except Exception as e:
        log_structured(
            storage_logger,
            logging.ERROR,
            "Error loading data",
            data_file_path=str(DATA_FILE_PATH),
            error=str(e),
            operation="load_error",
        )


def generate_slug(title: str) -> str:
    """Generate URL-friendly slug from title"""
    slug = re.sub(r"[^\w\s-]", "", title.lower())
    slug = re.sub(r"[-\s]+", "-", slug)
    return slug.strip("-")


def generate_token(user_id: str) -> str:
    """Generate JWT-like token (simplified)"""
    payload = f"{user_id}:{int(time.time())}"
    return f"token_{hashlib.sha256(payload.encode()).hexdigest()[:32]}"


def verify_token(token: str, storage: InMemoryStorage, client_ip: str = None) -> Optional[int]:
    """Verify token and return user_id if valid"""
    if not token or not token.startswith("token_"):
        if token:  # Only log if token was provided but invalid
            log_structured(
                security_logger,
                logging.WARNING,
                "Invalid token format",
                ip=client_ip,
                token_invalid=True,
                token_prefix=token[:10] if token else "None",
            )
        return None

    # in a real implementation, you'd decode the JWT => for simplicity, we'll store token->user_id mapping
    user_id = next((user_id for user_id, user in storage.users.items() if user.get("token") == token), None)

    if user_id is None:
        log_structured(
            security_logger,
            logging.WARNING,
            "Token verification failed",
            ip=client_ip,
            token_not_found=True,
            token_prefix=token[:10],
        )

    return user_id


def get_user_by_email(email: str, storage: InMemoryStorage) -> Optional[Dict]:
    """Find user by email"""
    return next((user for user in storage.users.values() if user["email"] == email), None)


def get_user_by_username(username: str, storage: InMemoryStorage) -> Optional[Dict]:
    """Find user by username"""
    return next((user for user in storage.users.values() if user["username"] == username), None)


def get_article_by_slug(slug: str, storage: InMemoryStorage) -> Optional[Dict]:
    """Find article by slug"""
    return next((article for article in storage.articles.values() if article["slug"] == slug), None)


def create_user_response(user: Dict, include_token: bool = True) -> Dict:
    """Create user response format"""
    response = {
        "username": user["username"],
        "email": user["email"],
        "bio": user.get("bio", ""),
        "image": user.get("image", DEMO_DATA_DEFAULT_IMAGE),
    }
    if include_token:
        response["token"] = user.get("token", "")
    return response


def create_profile_response(user: Dict, storage: InMemoryStorage, current_user_id: Optional[str] = None) -> Dict:
    """Create profile response format"""
    following = False
    if current_user_id:
        following = storage.follows.is_linked(current_user_id, user["id"])

    return {
        "username": user["username"],
        "bio": user.get("bio", ""),
        "image": user.get("image", DEMO_DATA_DEFAULT_IMAGE),
        "following": following,
    }


def create_article_response(article: Dict, storage: InMemoryStorage, current_user_id: Optional[str] = None) -> Dict:
    """Create article response format"""
    author = storage.users.get(article["author_id"])
    favorited = False
    if current_user_id:
        favorited = storage.favorites.is_linked(current_user_id, article["id"])

    favorites_count = len(storage.favorites.sources_for_target(article["id"]))

    return {
        "slug": article["slug"],
        "title": article["title"],
        "description": article["description"],
        "body": article["body"],
        "tagList": sorted(article["tagList"]),
        "createdAt": article["createdAt"],
        "updatedAt": article["updatedAt"],
        "favorited": favorited,
        "favoritesCount": favorites_count,
        "author": create_profile_response(author, storage, current_user_id),
    }


def create_comment_response(comment: Dict, storage: InMemoryStorage, current_user_id: Optional[str] = None) -> Dict:
    """Create comment response format"""
    author = storage.users.get(comment["author_id"])

    return {
        "id": comment["id"],
        "createdAt": comment["createdAt"],
        "updatedAt": comment["updatedAt"],
        "body": comment["body"],
        "author": create_profile_response(author, storage, current_user_id),
    }


class RealWorldHandler:
    """WSGI application for RealWorld API"""

    def __call__(self, environ, start_response):
        """WSGI application entry point"""
        try:
            self.environ = environ
            self.start_response = start_response
            self._request_start_time = time_ns()

            # Get request info
            self._request_method = environ["REQUEST_METHOD"]
            self._request_path = environ.get("PATH_INFO", "/")
            self._query_string = environ.get("QUERY_STRING", "")
            self._query_params = parse_qs(self._query_string) if self._query_string else {}
            # Get client IP
            client_ip = self._get_client_ip()
            # Get auth header
            auth_header = environ.get("HTTP_AUTHORIZATION", "")
            token = auth_header.replace("Token ", "") if auth_header.startswith("Token ") else None
            # Get storage (session identified by token, not cookie)
            self.target_session_id, self.storage = storage_container.get_storage(None, client_ip, token)
            # Set instance variables
            self.current_user_id = verify_token(token, self.storage, client_ip) if token else None
        except Exception as exc:
            log_structured(
                http_logger,
                logging.ERROR,
                "Internal Server Error",
                method=getattr(self, "_request_method", None),
                path=getattr(self, "_request_path", None),
                exception_type=str(exc),
                exception_traceback=format_tb(exc.__traceback__),
            )
            return self._send_error(500, "Internal Server Error")
        # Route request
        try:
            return self._handle_request(self._request_method, self._request_path, self._query_params)
        except Exception as exc:
            log_structured(
                http_logger,
                logging.ERROR,
                "Internal Server Error",
                method=self._request_method,
                path=self._request_path,
                exception_type=str(exc),
                exception_traceback=format_tb(exc.__traceback__),
            )
            return self._send_error(500, "Internal Server Error")

    @staticmethod
    def _require_auth(func):
        """Require authentication, only executes the wrapped method if current_user_id is not None, else returns 401"""

        def wrapper(self, *args, **kwargs):
            if self.current_user_id is None:
                client_ip = self._get_client_ip()
                log_structured(
                    security_logger,
                    logging.WARNING,
                    "Authentication required but not provided",
                    ip=client_ip,
                    auth_required=True,
                )
                return self._send_error(401, {"errors": {"body": ["Unauthorized"]}})
            return func(self, *args, **kwargs)

        return wrapper

    def _get_client_ip(self):
        """Get client IP address from header (if configured) or environ"""
        if CLIENT_IP_HEADER:
            header_key = f"HTTP_{CLIENT_IP_HEADER.upper().replace('-', '_')}"
            header_value = self.environ.get(header_key)
            if header_value:
                return header_value.split(",")[0].strip()
        return self.environ.get("REMOTE_ADDR", "127.0.0.1")

    def _handle_request(self, method: str, path: str, query_params: dict):
        """Route request to appropriate handler"""
        # Log request entry point
        log_structured(http_logger, logging.INFO, "Request received")

        # Remove leading slash and split path
        path_parts = path.strip("/").split("/")

        # Log request treatment start
        client_ip = self._get_client_ip()
        log_structured(
            http_logger,
            logging.INFO,
            "Request treatment started",
            method=method,
            path=path,
            ip=client_ip,
            user_id=self.current_user_id,
        )
        if PATH_PREFIX_PARTS:
            if not path_parts[: len(PATH_PREFIX_PARTS)] == PATH_PREFIX_PARTS:
                return self._send_error(404, {"errors": {"body": ["Not found"]}})
            path_parts = path_parts[len(PATH_PREFIX_PARTS) :]
        # Route to handlers
        if method == "POST" and path_parts == ["users"]:
            return self._handle_register(self.storage)
        elif method == "POST" and path_parts == ["users", "login"]:
            return self._handle_login(self.storage)
        elif method == "GET" and path_parts == ["user"]:
            return self._handle_get_current_user(self.storage)
        elif method == "PUT" and path_parts == ["user"]:
            return self._handle_update_user(self.storage)
        elif method == "GET" and path_parts[0] == "profiles" and len(path_parts) == 2:
            return self._handle_get_profile(self.storage, path_parts[1])
        elif method == "POST" and len(path_parts) == 3 and path_parts[0] == "profiles" and path_parts[2] == "follow":
            return self._handle_follow_user(self.storage, path_parts[1])
        elif method == "DELETE" and len(path_parts) == 3 and path_parts[0] == "profiles" and path_parts[2] == "follow":
            return self._handle_unfollow_user(self.storage, path_parts[1])
        elif method == "GET" and path_parts == ["articles"]:
            return self._handle_list_articles(self.storage, query_params)
        elif method == "GET" and path_parts == ["articles", "feed"]:
            return self._handle_articles_feed(self.storage, query_params)
        elif method == "POST" and path_parts == ["articles"]:
            return self._handle_create_article(self.storage)
        elif method == "GET" and len(path_parts) == 2 and path_parts[0] == "articles":
            return self._handle_get_article(self.storage, path_parts[1])
        elif method == "PUT" and len(path_parts) == 2 and path_parts[0] == "articles":
            return self._handle_update_article(self.storage, path_parts[1])
        elif method == "DELETE" and len(path_parts) == 2 and path_parts[0] == "articles":
            return self._handle_delete_article(self.storage, path_parts[1])
        elif method == "POST" and len(path_parts) == 3 and path_parts[0] == "articles" and path_parts[2] == "favorite":
            return self._handle_favorite_article(self.storage, path_parts[1])
        elif (
            method == "DELETE" and len(path_parts) == 3 and path_parts[0] == "articles" and path_parts[2] == "favorite"
        ):
            return self._handle_unfavorite_article(self.storage, path_parts[1])
        elif method == "GET" and len(path_parts) == 3 and path_parts[0] == "articles" and path_parts[2] == "comments":
            return self._handle_get_comments(self.storage, path_parts[1])
        elif method == "POST" and len(path_parts) == 3 and path_parts[0] == "articles" and path_parts[2] == "comments":
            return self._handle_create_comment(self.storage, path_parts[1])
        elif (
            method == "DELETE" and len(path_parts) == 4 and path_parts[0] == "articles" and path_parts[2] == "comments"
        ):
            return self._handle_delete_comment(self.storage, path_parts[1], int(path_parts[3]))
        elif method == "GET" and path_parts == ["tags"]:
            return self._handle_get_tags(self.storage)
        elif method == "OPTIONS":
            return self._handle_options()
        else:
            return self._send_error(404, {"errors": {"body": ["Not found"]}})

    def _handle_options(self):
        """Handle OPTIONS requests for CORS"""
        headers = [
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type, Authorization"),
        ]
        self.start_response("200 OK", headers)
        return [b""]

    def _get_request_body(self) -> Dict:
        """Parse JSON request body"""
        content_length = int(self.environ.get("CONTENT_LENGTH", 0))
        if content_length == 0:
            log_structured(
                http_logger, logging.DEBUG, "Request body: empty (no content-length)", payload_size=0, has_body=False
            )
            return {}
        body = self.environ["wsgi.input"].read(content_length).decode("utf-8")
        parsed_body = json.loads(body) if body else {}

        # Log detailed request payload at debug level
        client_ip = self._get_client_ip()
        log_structured(
            *(http_logger, logging.DEBUG, "Request body received"),
            **{"payload_size": content_length, "body_preview": body[:200], "ip": client_ip, "has_body": True},
            **{"content_length": content_length, "body_truncated": len(body) > 200},
        )
        return parsed_body

    def _send_response(
        self,
        status_code: int,
        data: Optional[Dict],
        start_time: int = None,
        method: str = None,
        path: str = None,
    ):
        """Send JSON response"""
        if data is not None:
            response_body = json.dumps(data, indent=2)
            response_body_bytes = response_body.encode("utf-8")
        else:
            response_body, response_body_bytes = None, b""
        # Prepare headers
        headers = [
            ("Content-Type", "application/json"),
            ("Access-Control-Allow-Origin", "*"),
            ("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type, Authorization"),
        ]
        # Send response
        status_reason = {200: "OK", 201: "Created", 204: "No Content", 400: "Bad Request", 404: "Not Found"}.get(
            status_code, "Error"
        )
        status_text = f"{status_code} {status_reason}"
        # Log response
        duration_ms = (time_ns() - start_time) / 1_000_000 if start_time else None
        client_ip = self._get_client_ip()
        response_size = len(response_body_bytes)
        log_structured(
            http_logger,
            logging.INFO,
            "Request completed",
            method=method,
            path=path,
            status_code=status_code,
            duration_ms=duration_ms,
            response_size=response_size,
            ip=client_ip,
        )
        self.start_response(status_text, headers)
        return [response_body_bytes] if response_body_bytes else []

    def _send_error(
        self, status_code: int, error_data: Dict, start_time: int = None, method: str = None, path: str = None
    ):
        """Send error response"""
        # Use stored request timing if available
        start_time = start_time or getattr(self, "_request_start_time", None)
        method = method or getattr(self, "_request_method", None)
        path = path or getattr(self, "_request_path", None)
        return self._send_response(status_code, error_data, start_time, method, path)

    def _send_response_with_timing(self, status_code: int, data: Dict):
        """Send response with automatic timing from stored request data"""
        start_time = getattr(self, "_request_start_time", None)
        method = getattr(self, "_request_method", None)
        path = getattr(self, "_request_path", None)
        return self._send_response(status_code, data, start_time, method, path)

    # Auth endpoints

    def _handle_register(self, storage: InMemoryStorage):
        """POST /users - Register new user"""
        data = self._get_request_body()
        user_data = data.get("user", {})
        email = user_data.get("email")
        username = user_data.get("username")
        password = user_data.get("password")
        client_ip = self._get_client_ip()
        if not all([email, username, password]):
            log_structured(
                auth_logger,
                logging.WARNING,
                "Registration failed: missing fields",
                ip=client_ip,
                email=email,
                username=username,
            )
            return self._send_error(422, {"errors": {"body": ["Email, username and password are required"]}})
        max_lens = ((email, MAX_LEN_USER_EMAIL), (username, MAX_LEN_USER_USERNAME), (password, MAX_LEN_USER_PASSWORD))
        if not all(type(d) is str for d in (email, username, password)) or not all(
            len(variable) <= max_allowed_length for variable, max_allowed_length in max_lens
        ):
            err_str = "Email, username and password are expected as strings of length less than "
            err_str += f"{MAX_LEN_USER_EMAIL}, {MAX_LEN_USER_USERNAME}, and {MAX_LEN_USER_PASSWORD}, respectively"
            log_structured(
                auth_logger,
                logging.WARNING,
                "Registration failed: invalid field lengths",
                ip=client_ip,
                email=email,
                username=username,
            )
            return self._send_error(422, {"errors": {"body": [err_str]}})
        # Check if user already exists
        if get_user_by_email(email, storage) or get_user_by_username(username, storage):
            log_structured(
                auth_logger,
                logging.WARNING,
                "Registration failed: user already exists",
                ip=client_ip,
                email=email,
                username=username,
            )
            return self._send_error(409, {"errors": {"body": ["User already exists"]}})
        # Create new user
        user = {
            "email": email,
            "username": username,
            "password": hash_password(password),
            "bio": "",
            "image": DEMO_DATA_DEFAULT_IMAGE,
            "createdAt": get_current_time(),
        }
        # Add user and get the auto-assigned ID
        user = storage.users.add(user)
        user_id = user["id"]
        # Generate token after we have the user_id
        token = generate_token(user_id)
        storage_container.bind_jwt_to_session_id(token, self.target_session_id)
        user["token"] = token
        log_structured(
            auth_logger,
            logging.INFO,
            "User registered successfully",
            ip=client_ip,
            email=email,
            username=username,
            user_id=user_id,
        )
        return self._send_response_with_timing(201, {"user": create_user_response(user)})

    def _handle_login(self, storage: InMemoryStorage):
        """POST /users/login - Login user"""
        data = self._get_request_body()
        user_data = data.get("user", {})
        email = user_data.get("email")
        password = user_data.get("password")
        client_ip = self._get_client_ip()
        if not all([email, password]):
            log_structured(auth_logger, logging.WARNING, "Login failed: missing fields", ip=client_ip, email=email)
            return self._send_error(422, {"errors": {"body": ["Email and password are required"]}})
        hashed_password = hash_password(password)
        # First, try to find user in current session's storage
        user = get_user_by_email(email, storage)
        target_session_id = self.target_session_id
        target_storage = storage
        # If not found in current session, search all sessions
        if not user or user["password"] != hashed_password:
            found_session_id, found_storage = storage_container.find_session_by_credentials(email, hashed_password)
            if found_storage:
                # User found in another session - create new session pointing to same storage
                target_session_id = str(uuid.uuid4())
                storage_container.push(time_ns(), target_session_id, data=found_storage, client_ip=client_ip)
                target_storage = found_storage
                user = get_user_by_email(email, target_storage)
                log_structured(
                    auth_logger,
                    logging.INFO,
                    "Login: Created new session for existing user",
                    ip=client_ip,
                    email=email,
                    original_session_id=found_session_id,
                    new_session_id=target_session_id,
                    user_id=user["id"],
                )
            else:
                log_structured(
                    auth_logger, logging.WARNING, "Login failed: invalid credentials", ip=client_ip, email=email
                )
                return self._send_error(401, {"errors": {"body": ["Invalid credentials"]}})
        user["token"] = generate_token(user["id"])  # Generate new token
        storage_container.bind_jwt_to_session_id(user["token"], target_session_id)
        log_structured(
            auth_logger,
            logging.INFO,
            "User logged in successfully",
            ip=client_ip,
            email=email,
            username=user.get("username"),
            user_id=user["id"],
        )
        return self._send_response_with_timing(200, {"user": create_user_response(user)})

    @_require_auth
    def _handle_get_current_user(self, storage: InMemoryStorage):
        """GET /user - Get current user"""
        user = storage.users.get(self.current_user_id)
        return self._send_response(200, {"user": create_user_response(user)})

    def _helper_update_user_field(self, source_dict, target_dict, name, max_len):
        """returns response if there is an error, None otherwise"""
        if name not in source_dict:
            return None
        if type(source_dict[name]) is not str or len(source_dict[name]) > max_len:
            err_str = f"{name} is an optional string of length <= {max_len}"
            return self._send_error(422, {"errors": {"body": [err_str]}})
        target_dict[name] = source_dict[name]
        return None

    @_require_auth
    def _handle_update_user(self, storage: InMemoryStorage):
        """PUT /user - Update current user"""
        data = self._get_request_body()
        user_data = data.get("user", {})
        user_update = {}
        # Update fields if provided
        error_response = (
            self._helper_update_user_field(user_data, user_update, "username", MAX_LEN_USER_USERNAME)
            or self._helper_update_user_field(user_data, user_update, "password", MAX_LEN_USER_PASSWORD)
            or self._helper_update_user_field(user_data, user_update, "bio", MAX_LEN_USER_BIO)
            or self._helper_update_user_field(user_data, user_update, "image", MAX_LEN_USER_IMAGE)
        )
        if error_response:
            return error_response
        if "password" in user_update:
            user_update["password"] = hash_password(user_update["password"])
        user = storage.users.get(self.current_user_id)
        user.update(**user_update)
        return self._send_response(200, {"user": create_user_response(user)})

    # Profile endpoints

    def _handle_get_profile(self, storage: InMemoryStorage, username: str):
        """GET /profiles/{username} - Get profile"""
        user = get_user_by_username(username, storage)
        if not user:
            return self._send_error(404, {"errors": {"body": ["Profile not found"]}})
        return self._send_response(200, {"profile": create_profile_response(user, storage, self.current_user_id)})

    @_require_auth
    def _handle_follow_user(self, storage: InMemoryStorage, username: str):
        """POST /profiles/{username}/follow - Follow user"""
        target_user = get_user_by_username(username, storage)
        if not target_user:
            return self._send_error(404, {"errors": {"body": ["Profile not found"]}})
        if target_user["id"] == self.current_user_id:
            return self._send_error(422, {"errors": {"body": ["Cannot follow yourself"]}})
        storage.follows.add(self.current_user_id, target_user["id"])
        # Log successful follow operation
        client_ip = self._get_client_ip()
        log_structured(
            http_logger,
            logging.INFO,
            "User followed",
            "CRUD",
            operation="follow_user",
            follower_id=self.current_user_id,
            followed_username=username,
            followed_id=target_user["id"],
            ip=client_ip,
        )
        return self._send_response_with_timing(
            200, {"profile": create_profile_response(target_user, storage, self.current_user_id)}
        )

    @_require_auth
    def _handle_unfollow_user(self, storage: InMemoryStorage, username: str):
        """DELETE /profiles/{username}/follow - Unfollow user"""
        target_user = get_user_by_username(username, storage)
        if not target_user:
            return self._send_error(404, {"errors": {"body": ["Profile not found"]}})
        storage.follows.remove(self.current_user_id, target_user["id"])
        # Log successful unfollow operation
        client_ip = self._get_client_ip()
        log_structured(
            http_logger,
            logging.INFO,
            "User unfollowed",
            "CRUD",
            operation="unfollow_user",
            follower_id=self.current_user_id,
            unfollowed_username=username,
            unfollowed_id=target_user["id"],
            ip=client_ip,
        )
        return self._send_response_with_timing(
            200, {"profile": create_profile_response(target_user, storage, self.current_user_id)}
        )

    # Article endpoints

    def _handle_list_articles(self, storage: InMemoryStorage, query_params: Dict):
        """GET /articles - List articles"""
        tag = query_params.get("tag", [None])[0]
        author = query_params.get("author", [None])[0]
        favorited = query_params.get("favorited", [None])[0]
        limit = int(query_params.get("limit", [20])[0])
        offset = int(query_params.get("offset", [0])[0])
        articles = list(storage.articles.values())
        # Filter by tag
        if tag:
            articles = [a for a in articles if tag in a["tagList"]]
        # Filter by author
        if author:
            author_user = get_user_by_username(author, storage)
            articles = [a for a in articles if a["author_id"] == author_user["id"]] if author_user else []
        # Filter by favorited
        if favorited:
            favorited_user = get_user_by_username(favorited, storage)
            if favorited_user:
                favorited_article_ids = storage.favorites.targets_for_source(favorited_user["id"])
                articles = [a for a in articles if a["id"] in favorited_article_ids]
            else:
                articles = []
        # Sort by creation date (newest first)
        articles.sort(key=lambda x: x["createdAt"], reverse=True)
        # Apply pagination
        total_count = len(articles)
        articles = articles[offset : offset + limit]
        # Format response
        article_responses = [create_article_response(a, storage, self.current_user_id) for a in articles]
        return self._send_response(200, {"articles": article_responses, "articlesCount": total_count})

    @_require_auth
    def _handle_articles_feed(self, storage: InMemoryStorage, query_params: Dict):
        """GET /articles/feed - Get feed of followed users"""
        limit = int(query_params.get("limit", [20])[0])
        offset = int(query_params.get("offset", [0])[0])
        followed_user_ids = storage.follows.targets_for_source(self.current_user_id)
        articles = [a for a in storage.articles.values() if a["author_id"] in followed_user_ids]
        # Sort by creation date (newest first)
        articles.sort(key=lambda x: x["createdAt"], reverse=True)
        # Apply pagination
        total_count = len(articles)
        articles = articles[offset : offset + limit]
        # Format response
        article_responses = [create_article_response(a, storage, self.current_user_id) for a in articles]
        return self._send_response(200, {"articles": article_responses, "articlesCount": total_count})

    def _helper_article_get_slug(self, storage, title):
        """ensure slug is unique"""
        slug = generate_slug(title)
        base_slug = slug
        counter = 1
        while get_article_by_slug(slug, storage):
            slug = f"{base_slug}-{counter}"
            counter += 1
        return slug

    def _helper_article_field(self, source_dict, name, max_len):
        """returns response if there is an error, None otherwise"""
        if name not in source_dict:
            return None
        if type(source_dict[name]) is not str or len(source_dict[name]) > max_len:
            err_str = f"{name} is an optional string of length <= {max_len}"
            return self._send_error(422, {"errors": {"body": [err_str]}})
        return None

    @_require_auth
    def _handle_create_article(self, storage: InMemoryStorage):
        """POST /articles - Create article"""
        data = self._get_request_body()
        article_data = data.get("article", {})
        title = article_data.get("title")
        description = article_data.get("description")
        body = article_data.get("body")
        if not all([title, description, body]):
            return self._send_error(422, {"errors": {"body": ["Title, description and body are required"]}})
        error_response = (
            self._helper_article_field(article_data, "title", MAX_LEN_ARTICLE_TITLE)
            or self._helper_article_field(article_data, "description", MAX_LEN_ARTICLE_DESCRIPTION)
            or self._helper_article_field(article_data, "body", MAX_LEN_ARTICLE_BODY)
        )
        if error_response:
            return error_response
        tag_list = article_data.get("tagList", [])
        if (
            type(tag_list) is not list
            or len(tag_list) > MAX_LEN_ARTICLE_TAG_LIST
            or any(type(e) is not str for e in tag_list)
            or any(len(e) > MAX_LEN_ARTICLE_TAG_LEN for e in tag_list)
        ):
            err_str = f"tagList is an optional list of less than {MAX_LEN_ARTICLE_TAG_LIST} strings "
            err_str += f"of less than {MAX_LEN_ARTICLE_TAG_LEN} chars"
            return self._send_error(422, {"errors": {"body": [err_str]}})
        slug = self._helper_article_get_slug(storage, title)
        # Create article
        current_time = get_current_time()
        article = {
            "slug": slug,
            "title": title,
            "description": description,
            "body": body,
            "tagList": sorted(tag_list),
            "author_id": self.current_user_id,
            "createdAt": current_time,
            "updatedAt": current_time,
        }
        storage.articles.add(article)
        # Log successful article creation
        client_ip = self._get_client_ip()
        log_structured(
            http_logger,
            logging.INFO,
            "Article created",
            "CRUD",
            operation="create_article",
            slug=article["slug"],
            title=article["title"],
            author_id=self.current_user_id,
            article_id=article["id"],
            ip=client_ip,
        )
        return self._send_response_with_timing(
            201, {"article": create_article_response(article, storage, self.current_user_id)}
        )

    def _handle_get_article(self, storage: InMemoryStorage, slug: str):
        """GET /articles/{slug} - Get article"""
        article = get_article_by_slug(slug, storage)
        if not article:
            return self._send_error(404, {"errors": {"body": ["Article not found"]}})
        return self._send_response(200, {"article": create_article_response(article, storage, self.current_user_id)})

    @_require_auth
    def _handle_update_article(self, storage: InMemoryStorage, slug: str):
        """PUT /articles/{slug} - Update article"""
        article = get_article_by_slug(slug, storage)
        if not article:
            return self._send_error(404, {"errors": {"body": ["Article not found"]}})
        if article["author_id"] != self.current_user_id:
            return self._send_error(403, {"errors": {"body": ["Forbidden"]}})
        data = self._get_request_body()
        article_data = data.get("article", {})
        article_update = {}  # update this intermediary dict to prevent half-finished updates
        # Update fields if provided
        if "title" in article_data and article["title"] != article_data["title"]:  # additional check for slug
            if error_response := self._helper_article_field(article_data, "title", MAX_LEN_ARTICLE_TITLE):
                return error_response
            article_update["title"] = article_data["title"]
            article_update["slug"] = self._helper_article_get_slug(storage, article["title"])
        if "description" in article_data:
            if error_response := self._helper_article_field(article_data, "description", MAX_LEN_ARTICLE_DESCRIPTION):
                return error_response
            article_update["description"] = article_data["description"]
        if "body" in article_data:
            if error_response := self._helper_article_field(article_data, "body", MAX_LEN_ARTICLE_BODY):
                return error_response
            article_update["body"] = article_data["body"]
        article_update["updatedAt"] = get_current_time()
        article.update(**article_update)
        # Log successful article update
        client_ip = self._get_client_ip()
        updated_fields = list(article_update.keys())
        log_structured(
            http_logger,
            logging.INFO,
            "Article updated",
            "CRUD",
            operation="update_article",
            slug=slug,
            updated_fields=updated_fields,
            author_id=self.current_user_id,
            article_id=article["id"],
            ip=client_ip,
        )
        return self._send_response_with_timing(
            200, {"article": create_article_response(article, storage, self.current_user_id)}
        )

    @_require_auth
    def _handle_delete_article(self, storage: InMemoryStorage, slug: str):
        """DELETE /articles/{slug} - Delete article"""
        article = get_article_by_slug(slug, storage)
        if not article:
            return self._send_error(404, {"errors": {"body": ["Article not found"]}})
        if article["author_id"] != self.current_user_id:
            return self._send_error(403, {"errors": {"body": ["Forbidden"]}})
        # Delete article and related data
        article_id = article["id"]
        storage.articles.delete(article_id)
        # Remove from favorites
        storage.favorites.delete_target(article_id)
        # Delete comments
        comments_to_delete = [c_id for c_id, c in storage.comments.items() if c["article_id"] == article_id]
        for c_id in comments_to_delete:
            storage.comments.delete(c_id)
        # Log successful article deletion
        client_ip = self._get_client_ip()
        log_structured(
            http_logger,
            logging.INFO,
            "Article deleted",
            "CRUD",
            operation="delete_article",
            slug=slug,
            article_id=article_id,
            author_id=self.current_user_id,
            deleted_comments_count=len(comments_to_delete),
            ip=client_ip,
        )
        # Send 204 No Content
        return self._send_response(204, None)

    @_require_auth
    def _handle_favorite_article(self, storage: InMemoryStorage, slug: str):
        """POST /articles/{slug}/favorite - Favorite article"""
        article = get_article_by_slug(slug, storage)
        if not article:
            return self._send_error(404, {"errors": {"body": ["Article not found"]}})
        storage.favorites.add(self.current_user_id, article["id"])
        return self._send_response(200, {"article": create_article_response(article, storage, self.current_user_id)})

    @_require_auth
    def _handle_unfavorite_article(self, storage: InMemoryStorage, slug: str):
        """DELETE /articles/{slug}/favorite - Unfavorite article"""
        article = get_article_by_slug(slug, storage)
        if not article:
            return self._send_error(404, {"errors": {"body": ["Article not found"]}})
        storage.favorites.remove(self.current_user_id, article["id"])
        return self._send_response(200, {"article": create_article_response(article, storage, self.current_user_id)})

    # Comment endpoints

    def _handle_get_comments(self, storage: InMemoryStorage, slug: str):
        """GET /articles/{slug}/comments - Get comments"""
        article = get_article_by_slug(slug, storage)
        if not article:
            return self._send_error(404, {"errors": {"body": ["Article not found"]}})
        comments = [c for c in storage.comments.values() if c["article_id"] == article["id"]]
        comments.sort(key=lambda x: x["createdAt"], reverse=True)
        comment_responses = [create_comment_response(c, storage, self.current_user_id) for c in comments]
        return self._send_response(200, {"comments": comment_responses})

    @_require_auth
    def _handle_create_comment(self, storage: InMemoryStorage, slug: str):
        """POST /articles/{slug}/comments - Create comment"""
        article = get_article_by_slug(slug, storage)
        if not article:
            return self._send_error(404, {"errors": {"body": ["Article not found"]}})
        data = self._get_request_body()
        comment_data = data.get("comment", {})
        body = comment_data.get("body")
        if not body:
            return self._send_error(422, {"errors": {"body": ["Body is required"]}})
        if type(body) is not str or len(body) > MAX_LEN_COMMENT_BODY:
            return self._send_error(
                422, {"errors": {"body": [f"Body is a string of less than {MAX_LEN_COMMENT_BODY} chars"]}}
            )
        # Create comment
        current_time = get_current_time()
        comment = {
            "body": body,
            "article_id": article["id"],
            "author_id": self.current_user_id,
            "createdAt": current_time,
            "updatedAt": current_time,
        }
        storage.comments.add(comment)
        # Log successful comment creation
        client_ip = self._get_client_ip()
        log_structured(
            http_logger,
            logging.INFO,
            "Comment created",
            "CRUD",
            operation="create_comment",
            comment_id=comment["id"],
            slug=slug,
            author_id=self.current_user_id,
            article_id=article["id"],
            ip=client_ip,
        )
        return self._send_response_with_timing(
            200, {"comment": create_comment_response(comment, storage, self.current_user_id)}
        )

    @_require_auth
    def _handle_delete_comment(self, storage: InMemoryStorage, slug: str, comment_id: int):
        """DELETE /articles/{slug}/comments/{id} - Delete comment"""
        article = get_article_by_slug(slug, storage)
        if not article:
            return self._send_error(404, {"errors": {"body": ["Article not found"]}})
        comment = storage.comments.get(comment_id)
        if not comment or comment["article_id"] != article["id"]:
            return self._send_error(404, {"errors": {"body": ["Comment not found"]}})
        # Only comment author or article author can delete
        if comment["author_id"] != self.current_user_id and article["author_id"] != self.current_user_id:
            return self._send_error(403, {"errors": {"body": ["Forbidden"]}})
        storage.comments.delete(comment_id)
        # Log successful comment deletion
        client_ip = self._get_client_ip()
        log_structured(
            http_logger,
            logging.INFO,
            "Comment deleted",
            "CRUD",
            operation="delete_comment",
            comment_id=comment_id,
            slug=slug,
            deleted_by_user_id=self.current_user_id,
            article_id=article["id"],
            ip=client_ip,
        )
        # Send 204 No Content
        return self._send_response(204, None)

    # Tag endpoints

    def _handle_get_tags(self, storage: InMemoryStorage):
        """GET /tags - Get all tags"""
        return self._send_response_with_timing(
            200, {"tags": sorted({t for a in storage.articles.values() for t in a.get("tagList", [])})}
        )


def run_server(port: int = 8000):
    """Run the RealWorld API server"""
    load_data()  # will load data if temp file found
    # Log server startup
    log_structured(lifecycle_logger, logging.INFO, "RealWorld API Server starting", port=port)
    # Log security configuration
    log_structured(
        config_logger,
        logging.INFO,
        "Security config",
        isolation_disabled=DISABLE_ISOLATION_MODE,
        max_sessions=MAX_SESSIONS,
    )
    # Log data persistence configuration
    log_structured(
        config_logger,
        logging.INFO,
        "Data persistence",
        data_persistence=bool(DATA_FILE_PATH),
        data_file_path=str(DATA_FILE_PATH) if DATA_FILE_PATH else None,
    )
    # Log resource limits
    log_structured(
        config_logger,
        logging.INFO,
        "Resource limits",
        max_users=MAX_USERS_PER_SESSION,
        max_articles=MAX_ARTICLES_PER_SESSION,
        max_comments=MAX_COMMENTS_PER_SESSION,
    )
    # Log estimated memory usage
    log_structured(
        config_logger,
        logging.INFO,
        "Estimated max memory used by data - x2 in reality due to overhead",
        max_memory_mb=NAIVE_SIZE_TOTAL / (1024 * 1024),
    )
    # Log logging configuration
    log_structured(
        config_logger,
        logging.INFO,
        "Logging config",
        log_level=LOG_LEVEL,
        log_file=bool(LOG_FILE),
        log_file_path=LOG_FILE,
    )
    # Create WSGI application
    app = RealWorldHandler()

    # Document routes - using print here
    print(f"RealWorld API Server running on http://localhost:{port}")
    print("API endpoints available:")
    print("  POST   /users  -------------------------- Register")
    print("  POST   /users/login  -------------------- Login")
    print("  GET    /user  --------------------------- Current user")
    print("  PUT    /user  --------------------------- Update user")
    print("  GET    /profiles/{username}  ------------ Get profile")
    print("  POST   /profiles/{username}/follow  ----- Follow user")
    print("  DELETE /profiles/{username}/follow  ----- Unfollow user")
    print("  GET    /articles  ----------------------- List articles")
    print("  GET    /articles/feed  ------------------ Get feed")
    print("  POST   /articles  ----------------------- Create article")
    print("  GET    /articles/{slug}  ---------------- Get article")
    print("  PUT    /articles/{slug}  ---------------- Update article")
    print("  DELETE /articles/{slug}  ---------------- Delete article")
    print("  POST   /articles/{slug}/favorite  ------- Favorite article")
    print("  DELETE /articles/{slug}/favorite  ------- Unfavorite article")
    print("  GET    /articles/{slug}/comments  ------- Get comments")
    print("  POST   /articles/{slug}/comments  ------- Create comment")
    print("  DELETE /articles/{slug}/comments/{id}  -- Delete comment")
    print("  GET    /tags  --------------------------- Get tags")
    print("\nPress Ctrl+C to stop the server")
    # Serve with waitress
    try:
        serve(app, host="0.0.0.0", port=port, threads=1)
    except KeyboardInterrupt:
        log_structured(lifecycle_logger, logging.INFO, "shutting down server")
        if DATA_FILE_PATH:
            log_structured(lifecycle_logger, logging.INFO, "trying to save data")
            did_save = save_data()
            log_structured(lifecycle_logger, logging.INFO, "saved data" if did_save else "couldn't save data")
        log_structured(lifecycle_logger, logging.INFO, "process terminating now")


if __name__ == "__main__":
    import sys

    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    run_server(port)


#### TESTS #############################################################################################################


class TestInMemoryModel(TestCase):
    def setUp(self):
        self.model = InMemoryModel(max_count=3)

    # init

    def test_init(self):
        model = InMemoryModel(max_count=5)
        self.assertEqual(model.max_count, 5)
        self.assertEqual(model.objects, {})
        self.assertEqual(model.last_accessed_ids, [])
        self.assertEqual(model.current_id_counter, 1)

    def test_negative_max_count(self):
        with self.assertRaises(ValueError) as exc:
            InMemoryModel(max_count=-1)
        self.assertEqual(str(exc.exception), "invalid value for max_count")

    # add

    def test_add_single_object(self):
        obj = {"name": "test"}
        result = self.model.add(obj)
        self.assertEqual(result, obj)
        self.assertIn("id", obj)
        self.assertEqual(obj["id"], "1")
        self.assertEqual(self.model.current_id_counter, 2)
        self.assertEqual(len(self.model.objects), 1)
        self.assertIn("1", self.model.objects)

    def test_add_multiple_objects(self):
        obj1 = {"name": "test1"}
        obj2 = {"name": "test2"}
        obj3 = {"name": "test3"}
        self.model.add(obj1)
        self.model.add(obj2)
        self.model.add(obj3)
        self.assertEqual(obj1["id"], "1")
        self.assertEqual(obj2["id"], "2")
        self.assertEqual(obj3["id"], "3")
        self.assertEqual(self.model.current_id_counter, 4)
        self.assertEqual(len(self.model.objects), 3)

    def test_add_with_auto_id(self):
        """an object with a set is id is then expected"""
        model = InMemoryModel(max_count=3)
        obj = {"name": "test"}
        result = model.add(obj)
        self.assertEqual(result, obj)
        self.assertEqual(obj["id"], "1")
        self.assertEqual(model.current_id_counter, 2)

    @patch("realworld_dummy_server.log_structured")
    def test_add_exceeds_max_count_will_overwrite(self, log_structured_mock):
        obj1 = {"name": "test1"}
        obj2 = {"name": "test2"}
        obj3 = {"name": "test3"}
        obj4 = {"name": "test4"}
        self.model.add(obj1)
        self.model.add(obj2)
        self.model.add(obj3)
        self.model.add(obj4)
        self.assertEqual(
            self.model.objects,
            {"2": {"name": "test2", "id": "2"}, "3": {"name": "test3", "id": "3"}, "4": {"name": "test4", "id": "4"}},
        )
        self.assertEqual(self.model.last_accessed_ids, ["2", "3", "4"])

    def test_add_dict_with_existing_id_key(self):
        # Test adding object that already has an "id" key
        obj = {"name": "test", "id": "existing_id"}
        self.model.add(obj)
        # Should overwrite the existing id
        self.assertEqual(obj["id"], "1")
        self.assertNotEqual(obj["id"], "existing_id")

    def test_current_id_counter_increments(self):
        initial_counter = self.model.current_id_counter
        self.model.add({"name": "test1"})
        self.assertEqual(self.model.current_id_counter, initial_counter + 1)
        self.model.add({"name": "test2"})
        self.assertEqual(self.model.current_id_counter, initial_counter + 2)

    # get

    def test_get_existing_object(self):
        obj = {"name": "test"}
        self.model.add(obj)
        retrieved = self.model.get("1")
        self.assertEqual(retrieved, obj)

    def test_get_nonexistent_object(self):
        result = self.model.get("999")
        self.assertIsNone(result)

    def test_get_with_int_id(self):
        obj = {"name": "test"}
        self.model.add(obj)
        retrieved = self.model.get(1)
        self.assertEqual(retrieved, obj)

    def test_get_with_invalid_id_type(self):
        with self.assertRaises(ValueError) as context:
            self.model.get({"invalid": "id"})
        self.assertIn("id must be an int or an str", str(context.exception))

    def test_get_with_long_id(self):
        long_id = "x" * (MAX_ID_LEN + 1)
        with self.assertRaises(ValueError) as context:
            self.model.get(long_id)
        self.assertIn("id is too long", str(context.exception))

    # keys / values / items

    def test_keys(self):
        self.model.add({"name": "test1"})
        self.model.add({"name": "test2"})
        keys = list(self.model.keys())
        self.assertEqual(sorted(keys), ["1", "2"])

    def test_values(self):
        self.model.add({"name": "test1"})
        self.model.add({"name": "test2"})
        self.assertEqual(list(self.model.values()), [{"id": "1", "name": "test1"}, {"id": "2", "name": "test2"}])

    def test_items(self):
        self.model.add({"name": "test1"})
        self.model.add({"name": "test2"})
        items = dict(self.model.items())
        self.assertEqual(items, {"1": {"id": "1", "name": "test1"}, "2": {"id": "2", "name": "test2"}})

    # delete

    def test_delete_existing_object_with_one_object(self):
        obj = {"name": "test"}
        self.model.add(obj)
        result = self.model.delete("1")
        self.assertTrue(result)
        self.assertEqual(len(self.model.objects), 0)

    def test_delete_existing_object_with_multiple_objects_deletes_first(self):
        self.model.add({"name": "test1"})
        self.model.add({"name": "test2"})
        self.model.add({"name": "test3"})
        self.assertEqual(self.model.current_id_counter, 4)
        result = self.model.delete("1")
        self.assertTrue(result)
        self.assertEqual(len(self.model.objects), 2)
        self.assertEqual(self.model.objects, {"2": {"name": "test2", "id": "2"}, "3": {"name": "test3", "id": "3"}})
        self.assertEqual(self.model.last_accessed_ids, ["2", "3"])

    def test_delete_existing_object_with_multiple_objects_deletes_middle(self):
        self.model.add({"name": "test1"})
        self.model.add({"name": "test2"})
        self.model.add({"name": "test3"})
        self.assertEqual(self.model.current_id_counter, 4)
        result = self.model.delete("2")
        self.assertTrue(result)
        self.assertEqual(len(self.model.objects), 2)
        self.assertEqual(self.model.objects, {"1": {"name": "test1", "id": "1"}, "3": {"name": "test3", "id": "3"}})
        self.assertEqual(self.model.last_accessed_ids, ["1", "3"])

    def test_delete_existing_object_with_multiple_objects_deletes_last(self):
        self.model.add({"name": "test1"})
        self.model.add({"name": "test2"})
        self.model.add({"name": "test3"})
        self.assertEqual(self.model.current_id_counter, 4)
        result = self.model.delete("3")
        self.assertTrue(result)
        self.assertEqual(len(self.model.objects), 2)
        self.assertEqual(self.model.objects, {"1": {"name": "test1", "id": "1"}, "2": {"name": "test2", "id": "2"}})
        self.assertEqual(self.model.last_accessed_ids, ["1", "2"])

    def test_delete_nonexistent_object(self):
        result = self.model.delete("999")
        self.assertFalse(result)

    def test_delete_with_int_id(self):
        obj = {"name": "test"}
        self.model.add(obj)
        result = self.model.delete(1)
        self.assertTrue(result)
        self.assertEqual(len(self.model.objects), 0)

    def test_delete_with_invalid_id_type(self):
        with self.assertRaises(ValueError) as context:
            self.model.delete({"invalid": "id"})
        self.assertIn("id must be an int or an str", str(context.exception))

    # mixed

    def test_max_id_length_exceeded(self):
        # Create a model with a very low MAX_ID_LEN to test the limit
        original_max_id_len = MAX_ID_LEN
        import realworld_dummy_server

        realworld_dummy_server.MAX_ID_LEN = 1
        try:
            model = InMemoryModel(max_count=10)
            # Add enough objects to reach the limit
            for i in range(9):  # IDs will be 1-9 (single digit)
                model.add({"name": f"test{i}"})
            # Adding the 10th object should trigger the error (ID would be "10", length 2)
            with self.assertRaises(ValueError) as context:
                model.add({"name": "test10"})
            self.assertIn("cannot allocate id: we reached MAX_ID_LEN limit", str(context.exception))
        finally:
            # Restore original value
            realworld_dummy_server.MAX_ID_LEN = original_max_id_len

    def test_empty_model_operations(self):
        # Test operations on empty model
        self.assertEqual(len(list(self.model.keys())), 0)
        self.assertEqual(len(list(self.model.values())), 0)
        self.assertEqual(len(list(self.model.items())), 0)
        self.assertIsNone(self.model.get("1"))
        self.assertFalse(self.model.delete("1"))

    def test_object_references_maintained(self):
        # Test that the same object reference is returned
        obj = {"name": "test", "data": [1, 2, 3]}
        self.model.add(obj)
        retrieved = self.model.get("1")
        self.assertIs(retrieved, obj)
        # Modify the original object
        obj["name"] = "modified"
        retrieved_again = self.model.get("1")
        self.assertEqual(retrieved_again["name"], "modified")

    def test_model_zero_max_count(self):
        with self.assertRaises(ValueError) as exc:
            InMemoryModel(max_count=0)
        self.assertEqual(str(exc.exception), "invalid value for max_count")

    @patch("realworld_dummy_server.log_structured")
    def test_one_max_count(self, log_structured_mock):
        model = InMemoryModel(max_count=1)
        obj1 = {"name": "test1"}
        obj2 = {"name": "test2"}
        model.add(obj1)
        self.assertEqual(len(model.objects), 1)
        model.add(obj2)
        self.assertEqual(len(model.objects), 1)
        self.assertIsNone(model.get("1"))
        remaining_obj = model.get("2")
        self.assertEqual(remaining_obj, obj2)

    def test_string_and_int_id_equivalence(self):
        self.model.add({"name": "test"})
        self.assertEqual(self.model.get("1"), {"id": "1", "name": "test"})
        self.assertEqual(self.model.get(1), {"id": "1", "name": "test"})

    def test_large_max_count(self):
        model = InMemoryModel(max_count=1000000)
        obj = {"name": "test"}
        model.add(obj)
        self.assertEqual(len(model.objects), 1)
        self.assertEqual(obj["id"], "1")

    def test_float_ids_rejected(self):
        with self.assertRaises(ValueError) as context:
            self.model.get(1.5)
        self.assertIn("id must be an int or an str", str(context.exception))

    def test_boolean_ids_rejected(self):
        with self.assertRaises(ValueError) as context:
            self.model.get(True)
        self.assertIn("id must be an int or an str", str(context.exception))

    def test_none_id_rejected(self):
        with self.assertRaises(ValueError) as context:
            self.model.get(None)
        self.assertIn("id must be an int or an str", str(context.exception))

    def test_list_id_rejected(self):
        with self.assertRaises(ValueError) as context:
            self.model.get([1, 2, 3])
        self.assertIn("id must be an int or an str", str(context.exception))

    def test_empty_string_id(self):
        obj = {"name": "test"}
        self.model.add(obj)
        # Empty string should be valid
        result = self.model.get("")
        self.assertIsNone(result)  # Won't match "1"

    def test_zero_string_id(self):
        result = self.model.get("0")
        self.assertIsNone(result)

    def test_zero_int_id(self):
        result = self.model.get(0)
        self.assertIsNone(result)

    def test_negative_int_id(self):
        result = self.model.get(-1)
        self.assertIsNone(result)

    def test_very_large_int_id(self):
        import sys

        very_large_id = sys.maxsize
        result = self.model.get(very_large_id)
        self.assertIsNone(result)

    def test_unicode_string_id(self):
        unicode_id = "测试"
        result = self.model.get(unicode_id)
        self.assertIsNone(result)

    def test_max_id_len_boundary(self):
        boundary_id = "x" * MAX_ID_LEN
        result = self.model.get(boundary_id)
        self.assertIsNone(result)

    def test_model_state_after_failed_operations(self):
        # Test that model state remains consistent after failed operations
        obj1 = {"name": "test1"}
        self.model.add(obj1)
        initial_objects_count = len(self.model.objects)
        initial_counter = self.model.current_id_counter
        # Try invalid operations
        try:
            self.model.get({"invalid": "id"})
        except ValueError:
            pass
        try:
            self.model.delete({"invalid": "id"})
        except ValueError:
            pass
        # State should be unchanged
        self.assertEqual(len(self.model.objects), initial_objects_count)
        self.assertEqual(self.model.current_id_counter, initial_counter)

    @patch("realworld_dummy_server.log_structured")
    def test_concurrent_access_simulation(self, log_structured_mock):
        # Simulate concurrent access patterns
        objects = []
        for i in range(5):
            obj = {"name": f"test{i}", "value": i * 10}
            self.model.add(obj)
            objects.append(obj)
        # Simulate multiple readers
        for i in range(0, 2):
            retrieved = self.model.get(str(i + 1))
            self.assertIsNone(retrieved)
        for i in range(2, 5):
            retrieved = self.model.get(str(i + 1))
            self.assertEqual(retrieved["name"], f"test{i}")
            self.assertEqual(retrieved["value"], i * 10)
        # Verify all objects still accessible
        self.assertEqual(len(list(self.model.keys())), 3)  # max_count is 3


class TestInMemoryLinks(TestCase):
    def setUp(self):
        self.links = InMemoryLinks(max_count=3)

    def test_init(self):
        links = InMemoryLinks(max_count=5)
        self.assertEqual(links.max_count, 5)
        self.assertEqual(links.links, [])

    def test_add_single_link(self):
        self.links.add("1", "2")
        self.assertEqual(self.links.links, [("1", "2")])

    def test_add_multiple_links(self):
        self.links.add("1", "2")
        self.links.add("2", "3")
        self.links.add("3", "4")
        self.assertEqual(self.links.links, [("1", "2"), ("2", "3"), ("3", "4")])

    def test_add_duplicate_link_moves_to_end(self):
        self.links.add("1", "2")
        self.links.add("2", "3")
        self.links.add("1", "2")  # Duplicate
        self.assertEqual(self.links.links, [("2", "3"), ("1", "2")])

    @patch("realworld_dummy_server.log_structured")
    def test_add_exceeds_max_count_removes_oldest(self, log_structured_mock):
        self.links.add("1", "2")
        self.links.add("2", "3")
        self.links.add("3", "4")
        self.links.add("4", "5")  # Should remove ("1", "2")
        self.assertEqual(self.links.links, [("2", "3"), ("3", "4"), ("4", "5")])

    def test_add_duplicate_when_at_max_count(self):
        self.links.add("1", "2")
        self.links.add("2", "3")
        self.links.add("3", "4")
        self.links.add("2", "3")  # Duplicate when at max
        self.assertEqual(self.links.links, [("1", "2"), ("3", "4"), ("2", "3")])

    def test_remove_existing_link(self):
        self.links.add("1", "2")
        self.links.add("2", "3")
        self.links.remove("1", "2")
        self.assertEqual(self.links.links, [("2", "3")])

    def test_remove_nonexistent_link(self):
        self.links.add("1", "2")
        self.links.remove("3", "4")  # Doesn't exist
        self.assertEqual(self.links.links, [("1", "2")])

    def test_remove_from_empty_links(self):
        self.links.remove("1", "2")
        self.assertEqual(self.links.links, [])

    def test_remove_middle_link(self):
        self.links.add("1", "2")
        self.links.add("2", "3")
        self.links.add("3", "4")
        self.links.remove("2", "3")
        self.assertEqual(self.links.links, [("1", "2"), ("3", "4")])

    def test_link_zero_max_count(self):
        links = InMemoryLinks(max_count=0)
        links.add("1", "2")
        self.assertEqual(links.links, [])

    @patch("realworld_dummy_server.log_structured")
    def test_one_max_count(self, log_structured_mock):
        links = InMemoryLinks(max_count=1)
        links.add("1", "2")
        links.add("2", "3")
        self.assertEqual(links.links, [("2", "3")])

    def test_mixed_operations(self):
        self.links.add("1", "2")
        self.links.add("2", "3")
        self.links.remove("1", "2")
        self.links.add("3", "4")
        self.links.add("4", "5")
        self.assertEqual(self.links.links, [("2", "3"), ("3", "4"), ("4", "5")])

    def test_add_same_link_multiple_times(self):
        self.links.add("1", "2")
        self.links.add("1", "2")
        self.links.add("1", "2")
        self.assertEqual(self.links.links, [("1", "2")])

    def test_edge_case_same_source_and_target(self):
        self.links.add("1", "1")
        self.assertEqual(self.links.links, [("1", "1")])
        self.links.remove("1", "1")
        self.assertEqual(self.links.links, [])

    def test_add_int_converts_to_str(self):
        self.links.add(1, 2)
        self.assertEqual(self.links.links, [("1", "2")])

    def test_add_boolean_raises_error(self):
        with self.assertRaises(ValueError) as context:
            self.links.add(True, False)
        self.assertIn("id must be an int or an str", str(context.exception))

    def test_add_dict_raises_error(self):
        with self.assertRaises(ValueError) as context:
            self.links.add({"key": "value"}, {"other": "data"})
        self.assertIn("id must be an int or an str", str(context.exception))

    def test_is_linked_empty_links(self):
        self.assertFalse(self.links.is_linked("1", "2"))

    def test_is_linked_existing_link(self):
        self.links.add("1", "2")
        self.assertTrue(self.links.is_linked("1", "2"))

    def test_is_linked_nonexistent_link(self):
        self.links.add("1", "2")
        self.assertFalse(self.links.is_linked("2", "1"))
        self.assertFalse(self.links.is_linked("1", "3"))

    def test_is_linked_with_int_ids(self):
        self.links.add(1, 2)
        self.assertTrue(self.links.is_linked(1, 2))
        self.assertTrue(self.links.is_linked("1", "2"))

    def test_is_linked_after_removal(self):
        self.links.add("1", "2")
        self.assertTrue(self.links.is_linked("1", "2"))
        self.links.remove("1", "2")
        self.assertFalse(self.links.is_linked("1", "2"))

    def test_targets_for_source_empty_links(self):
        self.assertEqual(self.links.targets_for_source("1"), [])

    def test_targets_for_source_no_matches(self):
        self.links.add("1", "2")
        self.links.add("2", "3")
        self.assertEqual(self.links.targets_for_source("3"), [])

    def test_targets_for_source_single_target(self):
        self.links.add("1", "2")
        self.assertEqual(self.links.targets_for_source("1"), ["2"])

    def test_targets_for_source_multiple_targets(self):
        self.links.add("1", "2")
        self.links.add("1", "3")
        self.links.add("1", "4")
        targets = self.links.targets_for_source("1")
        self.assertEqual(sorted(targets), ["2", "3", "4"])

    def test_targets_for_source_mixed_sources(self):
        self.links.add("1", "2")
        self.links.add("2", "3")
        self.links.add("1", "4")
        self.assertEqual(sorted(self.links.targets_for_source("1")), ["2", "4"])
        self.assertEqual(self.links.targets_for_source("2"), ["3"])

    def test_targets_for_source_with_int_id(self):
        self.links.add(1, 2)
        self.links.add(1, 3)
        targets = self.links.targets_for_source(1)
        self.assertEqual(sorted(targets), ["2", "3"])

    def test_sources_for_target_empty_links(self):
        self.assertEqual(self.links.sources_for_target("1"), [])

    def test_sources_for_target_no_matches(self):
        self.links.add("1", "2")
        self.links.add("2", "3")
        self.assertEqual(self.links.sources_for_target("1"), [])

    def test_sources_for_target_single_source(self):
        self.links.add("1", "2")
        self.assertEqual(self.links.sources_for_target("2"), ["1"])

    def test_sources_for_target_multiple_sources(self):
        self.links.add("1", "4")
        self.links.add("2", "4")
        self.links.add("3", "4")
        sources = self.links.sources_for_target("4")
        self.assertEqual(sorted(sources), ["1", "2", "3"])

    def test_sources_for_target_mixed_targets(self):
        self.links.add("1", "2")
        self.links.add("3", "2")
        self.links.add("1", "4")
        self.assertEqual(sorted(self.links.sources_for_target("2")), ["1", "3"])
        self.assertEqual(self.links.sources_for_target("4"), ["1"])

    def test_sources_for_target_with_int_id(self):
        self.links.add(1, 3)
        self.links.add(2, 3)
        sources = self.links.sources_for_target(3)
        self.assertEqual(sorted(sources), ["1", "2"])

    def test_delete_source_empty_links(self):
        self.links.delete_source("1")
        self.assertEqual(self.links.links, [])

    def test_delete_source_single_match(self):
        self.links.add("1", "2")
        self.links.add("3", "4")
        self.links.delete_source("1")
        self.assertEqual(self.links.links, [("3", "4")])

    def test_delete_source_multiple_matches(self):
        self.links.add("1", "2")
        self.links.add("1", "3")
        self.links.add("2", "4")
        self.links.delete_source("1")
        self.assertEqual(self.links.links, [("2", "4")])

    def test_delete_source_no_matches(self):
        self.links.add("1", "2")
        self.links.add("3", "4")
        original_links = self.links.links[:]
        self.links.delete_source("5")
        self.assertEqual(self.links.links, original_links)

    def test_delete_source_with_int_id(self):
        self.links.add(1, 2)
        self.links.add(3, 4)
        self.links.delete_source(1)
        self.assertEqual(self.links.links, [("3", "4")])

    def test_delete_target_empty_links(self):
        self.links.delete_target("1")
        self.assertEqual(self.links.links, [])

    def test_delete_target_single_match(self):
        self.links.add("1", "2")
        self.links.add("3", "4")
        self.links.delete_target("2")
        self.assertEqual(self.links.links, [("3", "4")])

    def test_delete_target_multiple_matches(self):
        self.links.add("1", "4")
        self.links.add("2", "4")
        self.links.add("3", "5")
        self.links.delete_target("4")
        self.assertEqual(self.links.links, [("3", "5")])

    def test_delete_target_no_matches(self):
        self.links.add("1", "2")
        self.links.add("3", "4")
        original_links = self.links.links[:]
        self.links.delete_target("5")
        self.assertEqual(self.links.links, original_links)

    def test_delete_target_with_int_id(self):
        self.links.add(1, 2)
        self.links.add(3, 4)
        self.links.delete_target(2)
        self.assertEqual(self.links.links, [("3", "4")])


class TestStorageContainer(TestCase):
    # Setup

    def setUp(self):
        self.container = _StorageContainer(disable_isolation_mode=False)

    # Helpers

    def _verify_heap_property(self, container):
        # Helper to verify min-heap property for a given container
        for i in range(len(container.heap)):
            left_child = 2 * i + 1
            right_child = 2 * i + 2
            if left_child < len(container.heap):
                self.assertLessEqual(
                    container.heap[i][0],
                    container.heap[left_child][0],
                    f"Heap property violated at index {i} and left child {left_child}",
                )
            if right_child < len(container.heap):
                self.assertLessEqual(
                    container.heap[i][0],
                    container.heap[right_child][0],
                    f"Heap property violated at index {i} and right child {right_child}",
                )

    def _verify_index_consistency(self, container):
        # Helper to verify index_map consistency with heap for a given container
        self.assertEqual(len(container.index_map), len(container.heap))
        for item_id, index in container.index_map.items():
            # Index should be valid
            self.assertGreaterEqual(index, 0)
            self.assertLess(index, len(container.heap))
            # Heap item at index should match
            heap_item = container.heap[index]
            self.assertEqual(heap_item[1], item_id, f"Index map inconsistency for {item_id}")
            self.assertEqual(heap_item[3], index, f"Internal index inconsistency for {item_id}")
        # Every heap item should be in index_map
        for i, heap_item in enumerate(container.heap):
            item_id = heap_item[1]
            self.assertIn(item_id, container.index_map)
            self.assertEqual(container.index_map[item_id], i)

    # Tests

    def test_heap_push_single_item(self):
        self.container._push(5, "item1", "data1")
        self.assertEqual(len(self.container.heap), 1)
        self.assertEqual(self.container.heap[0], [5, "item1", "data1", 0, None])
        self.assertEqual(self.container.index_map["item1"], 0)

    def test_heap_push_multiple_items_maintains_min_heap(self):
        self.container._push(10, "item1", "data1")
        self.container._push(5, "item2", "data2")
        self.container._push(15, "item3", "data3")
        self.container._push(3, "item4", "data4")
        # Root should be the minimum
        self.assertEqual(self.container.heap[0][0], 3)
        self.assertEqual(self.container.heap[0][1], "item4")
        # Verify heap property: parent <= children
        for i in range(len(self.container.heap)):
            left_child = 2 * i + 1
            right_child = 2 * i + 2
            if left_child < len(self.container.heap):
                self.assertLessEqual(self.container.heap[i][0], self.container.heap[left_child][0])
            if right_child < len(self.container.heap):
                self.assertLessEqual(self.container.heap[i][0], self.container.heap[right_child][0])

    def test_heap_pop_empty_heap(self):
        result = self.container._pop()
        self.assertIsNone(result)

    def test_heap_pop_single_item(self):
        self.container._push(5, "item1", "data1")
        result = self.container._pop()
        self.assertEqual(result, [5, "item1", "data1", 0, None])
        self.assertEqual(len(self.container.heap), 0)
        self.assertNotIn("item1", self.container.index_map)

    def test_heap_pop_multiple_items_returns_min(self):
        items = [(10, "item1"), (5, "item2"), (15, "item3"), (3, "item4"), (7, "item5")]
        for priority, item_id in items:
            self.container._push(priority, item_id, f"data_{item_id}")
        # Pop items should come out in priority order
        result1 = self.container._pop()
        self.assertEqual(result1[0], 3)  # minimum priority
        self.assertEqual(result1[1], "item4")
        result2 = self.container._pop()
        self.assertEqual(result2[0], 5)
        self.assertEqual(result2[1], "item2")
        # Verify heap property is maintained after pops
        for i in range(len(self.container.heap)):
            left_child = 2 * i + 1
            right_child = 2 * i + 2
            if left_child < len(self.container.heap):
                self.assertLessEqual(self.container.heap[i][0], self.container.heap[left_child][0])
            if right_child < len(self.container.heap):
                self.assertLessEqual(self.container.heap[i][0], self.container.heap[right_child][0])

    def test_heap_pop_multiple_items_pop_from_lowest_to_highest(self):
        base_ordering = (10, 5, 15, 0, 20, 11, 6, 16, 1, 21, 12, 7, 17, 2, 22)
        for i in base_ordering:
            self.container._push(i, f"item{i}", f"data_{i}")
        poppeds = []
        for i in range(len(base_ordering) + 5):
            self._verify_heap_property(self.container)
            self._verify_index_consistency(self.container)
            poppeds.append(self.container._pop())
        self.assertEqual(
            poppeds, [*([i, f"item{i}", f"data_{i}", 0, None] for i in sorted(base_ordering)), *([None] * 5)]
        )

    def test_update_priority_increase(self):
        self.container._push(5, "item1", "data1")
        self.container._push(10, "item2", "data2")
        self.container._push(15, "item3", "data3")
        # Increase priority of root element
        self.container._update_priority("item1", 20)
        # Root should no longer be item1
        self.assertNotEqual(self.container.heap[0][1], "item1")
        # Verify heap property is maintained
        for i in range(len(self.container.heap)):
            left_child = 2 * i + 1
            right_child = 2 * i + 2
            if left_child < len(self.container.heap):
                self.assertLessEqual(self.container.heap[i][0], self.container.heap[left_child][0])
            if right_child < len(self.container.heap):
                self.assertLessEqual(self.container.heap[i][0], self.container.heap[right_child][0])

    def test_update_priority_decrease(self):
        self.container._push(15, "item1", "data1")
        self.container._push(10, "item2", "data2")
        self.container._push(20, "item3", "data3")
        # Decrease priority of last element to make it root
        self.container._update_priority("item3", 1)
        # Root should now be item3
        self.assertEqual(self.container.heap[0][1], "item3")
        self.assertEqual(self.container.heap[0][0], 1)

    def test_update_priority_nonexistent_item(self):
        self.container._push(5, "item1", "data1")
        with self.assertRaises(ValueError) as context:
            self.container._update_priority("nonexistent", 10)
        self.assertIn("not found in heap", str(context.exception))

    def test_heap_index_consistency(self):
        # Test that index_map stays consistent with heap positions
        items = [(10, "a"), (5, "b"), (15, "c"), (3, "d"), (7, "e"), (12, "f")]
        for priority, item_id in items:
            self.container._push(priority, item_id, f"data_{item_id}")
        # Verify all items are in index_map
        for _, item_id in items:
            self.assertIn(item_id, self.container.index_map)
        # Verify index_map points to correct positions
        for item_id, index in self.container.index_map.items():
            self.assertEqual(self.container.heap[index][1], item_id)
            self.assertEqual(self.container.heap[index][3], index)
        # Pop some items and verify consistency
        self.container._pop()
        self.container._pop()
        # Re-verify consistency
        for item_id, index in self.container.index_map.items():
            self.assertEqual(self.container.heap[index][1], item_id)
            self.assertEqual(self.container.heap[index][3], index)

    def test_sift_operations_maintain_heap_property(self):
        # Test internal sift operations
        self.container.heap = [[10, "a", "data_a", 0], [5, "b", "data_b", 1], [15, "c", "data_c", 2]]
        self.container.index_map = {"a": 0, "b": 1, "c": 2}
        # Manually trigger sift_up (simulating priority decrease)
        self.container.heap[2][0] = 1  # Change priority
        self.container._sift_up(2)
        # Verify heap property
        for i in range(len(self.container.heap)):
            left_child = 2 * i + 1
            right_child = 2 * i + 2
            if left_child < len(self.container.heap):
                self.assertLessEqual(self.container.heap[i][0], self.container.heap[left_child][0])
            if right_child < len(self.container.heap):
                self.assertLessEqual(self.container.heap[i][0], self.container.heap[right_child][0])

    def test_swap_operation(self):
        self.container._push(10, "item1", "data1")
        self.container._push(5, "item2", "data2")
        # Test swap operation
        orig_item1_pos = self.container.index_map["item1"]
        orig_item2_pos = self.container.index_map["item2"]
        self.container._swap(0, 1)
        # Verify positions swapped
        self.assertEqual(self.container.index_map["item1"], orig_item2_pos)
        self.assertEqual(self.container.index_map["item2"], orig_item1_pos)
        # Verify heap items swapped
        self.assertEqual(self.container.heap[orig_item2_pos][1], "item1")
        self.assertEqual(self.container.heap[orig_item1_pos][1], "item2")
        # Verify internal indices updated
        self.assertEqual(self.container.heap[orig_item2_pos][3], orig_item2_pos)
        self.assertEqual(self.container.heap[orig_item1_pos][3], orig_item1_pos)

    def test_heap_with_duplicate_priorities(self):
        # Test heap behavior with duplicate priorities
        self.container._push(5, "item1", "data1")
        self.container._push(5, "item2", "data2")
        self.container._push(5, "item3", "data3")
        self.container._push(3, "item4", "data4")
        self.container._push(5, "item5", "data5")
        # Root should be minimum priority
        self.assertEqual(self.container.heap[0][0], 3)
        self.assertEqual(self.container.heap[0][1], "item4")
        # Verify heap property with duplicates
        self._verify_heap_property(self.container)
        self._verify_index_consistency(self.container)
        # Pop minimum and verify heap still valid
        result = self.container._pop()
        self.assertEqual(result[0], 3)
        self._verify_heap_property(self.container)
        self._verify_index_consistency(self.container)

    def test_update_priority_to_same_value(self):
        # Test updating priority to the same value (should be no-op)
        self.container._push(10, "item1", "data1")
        self.container._push(5, "item2", "data2")
        original_heap = [item[:] for item in self.container.heap]  # Deep copy
        original_index_map = self.container.index_map.copy()
        self.container._update_priority("item1", 10)  # Same priority
        # Heap should be unchanged
        self.assertEqual(len(self.container.heap), len(original_heap))
        self.assertEqual(self.container.index_map, original_index_map)
        self._verify_heap_property(self.container)
        self._verify_index_consistency(self.container)

    def test_pop_all_items_sequential(self):
        # Test popping all items from heap
        priorities = [15, 3, 8, 1, 12, 6, 20, 4]
        items = []
        for i, priority in enumerate(priorities):
            item_id = f"item{i}"
            self.container._push(priority, item_id, f"data{i}")
            items.append((priority, item_id))
        # Pop all items and verify they come out in sorted order
        popped_priorities = []
        while len(self.container.heap) > 0:
            self._verify_heap_property(self.container)
            self._verify_index_consistency(self.container)
            result = self.container._pop()
            popped_priorities.append(result[0])
        # Should be in ascending order
        self.assertEqual(popped_priorities, sorted([p for p, _ in items]))
        self.assertEqual(len(self.container.heap), 0)
        self.assertEqual(len(self.container.index_map), 0)

    def test_mixed_operations_consistency(self):
        # Test mix of push, pop, and update operations
        self.container._push(10, "a", "data_a")
        self.container._push(5, "b", "data_b")
        self.container._push(15, "c", "data_c")
        self._verify_heap_property(self.container)
        self._verify_index_consistency(self.container)
        # Update priority
        self.container._update_priority("c", 1)
        self._verify_heap_property(self.container)
        self._verify_index_consistency(self.container)
        self.assertEqual(self.container.heap[0][1], "c")  # Should be new root
        # Pop minimum
        result = self.container._pop()
        self.assertEqual(result[1], "c")
        self._verify_heap_property(self.container)
        self._verify_index_consistency(self.container)
        # Add more items
        self.container._push(3, "d", "data_d")
        self.container._push(8, "e", "data_e")
        self._verify_heap_property(self.container)
        self._verify_index_consistency(self.container)
        # Update existing item
        self.container._update_priority("b", 20)
        self._verify_heap_property(self.container)
        self._verify_index_consistency(self.container)

    def test_empty_heap_edge_cases(self):
        # Test operations on empty heap
        self.assertEqual(len(self.container.heap), 0)
        self.assertEqual(len(self.container.index_map), 0)
        # Pop from empty heap
        result = self.container._pop()
        self.assertIsNone(result)
        # Update non-existent item
        with self.assertRaises(ValueError):
            self.container._update_priority("nonexistent", 10)

    def test_large_heap_operations(self):
        # Stress test with many items
        import random

        random.seed(42)  # For reproducible tests
        items = []
        num_items = 100
        # Push many items
        for i in range(num_items):
            priority = random.randint(1, 1000)
            item_id = f"item_{i}"
            self.container._push(priority, item_id, f"data_{i}")
            items.append((priority, item_id))
            # Verify heap property periodically
            if i % 20 == 0:
                self._verify_heap_property(self.container)
                self._verify_index_consistency(self.container)
        # Final verification
        self._verify_heap_property(self.container)
        self._verify_index_consistency(self.container)
        self.assertEqual(len(self.container.heap), num_items)
        self.assertEqual(len(self.container.index_map), num_items)
        # Update random items
        for _ in range(20):
            item_idx = random.randint(0, num_items - 1)
            item_id = f"item_{item_idx}"
            new_priority = random.randint(1, 1000)
            self.container._update_priority(item_id, new_priority)
            self._verify_heap_property(self.container)
            self._verify_index_consistency(self.container)

    def test_boundary_priorities(self):
        # Test with extreme priority values
        import sys

        # Test with very large and small numbers
        self.container._push(sys.maxsize, "max_item", "max_data")
        self.container._push(-sys.maxsize, "min_item", "min_data")
        self.container._push(0, "zero_item", "zero_data")
        self._verify_heap_property(self.container)
        self._verify_index_consistency(self.container)
        # Min should be at root
        self.assertEqual(self.container.heap[0][0], -sys.maxsize)
        self.assertEqual(self.container.heap[0][1], "min_item")
        # Pop and verify order
        result1 = self.container._pop()
        self.assertEqual(result1[0], -sys.maxsize)
        result2 = self.container._pop()
        self.assertEqual(result2[0], 0)
        result3 = self.container._pop()
        self.assertEqual(result3[0], sys.maxsize)

    def test_special_character_item_ids(self):
        # Test with various item ID formats
        special_ids = [
            "item-with-dashes",
            "item_with_underscores",
            "item.with.dots",
            "item with spaces",
            "item@with#symbols",
            "123numeric_start",
            "",  # empty string
            "🎯emoji_id",
            "very_long_" + "x" * 100 + "_id",
        ]
        for i, item_id in enumerate(special_ids):
            self.container._push(i + 1, item_id, f"data_{i}")
        self._verify_heap_property(self.container)
        self._verify_index_consistency(self.container)
        # Update some items
        self.container._update_priority("item-with-dashes", 50)
        self.container._update_priority("🎯emoji_id", 0)
        self._verify_heap_property(self.container)
        self._verify_index_consistency(self.container)

    def test_heap_after_multiple_updates(self):
        # Test heap consistency after many priority updates
        items = ["a", "b", "c", "d", "e", "f"]
        priorities = [10, 20, 30, 40, 50, 60]
        for item_id, priority in zip(items, priorities):
            self.container._push(priority, item_id, f"data_{item_id}")
        # Perform multiple updates that should change heap structure
        updates = [
            ("f", 1),  # Move last to first
            ("a", 100),  # Move first to last
            ("c", 15),  # Minor adjustment
            ("e", 5),  # Move middle to near front
        ]
        for item_id, new_priority in updates:
            self.container._update_priority(item_id, new_priority)
            self._verify_heap_property(self.container)
            self._verify_index_consistency(self.container)
        # Verify final order by popping all
        popped_items = []
        while self.container.heap:
            result = self.container._pop()
            popped_items.append((result[0], result[1]))
            self._verify_heap_property(self.container)
            self._verify_index_consistency(self.container)
        # Should be in priority order
        popped_priorities = [priority for priority, _ in popped_items]
        self.assertEqual(popped_priorities, sorted(popped_priorities))

    def test_sift_operations_edge_cases(self):
        # Test sift operations at heap boundaries
        # Single item - sift operations should be no-ops
        self.container._push(5, "single", "data")
        original_heap = [item[:] for item in self.container.heap]
        self.container._sift_up(0)
        self.container._sift_down(0)
        self.assertEqual(self.container.heap, original_heap)
        # Two items
        self.container._push(10, "second", "data2")
        self._verify_heap_property(self.container)
        # Manually test sift operations
        if self.container.heap[1][0] < self.container.heap[0][0]:
            self.container._swap(0, 1)
        self._verify_heap_property(self.container)
        self._verify_index_consistency(self.container)

    @patch("realworld_dummy_server.log_structured")
    def test_max_sessions_is_working_with_a_continuous_sequence(self, log_structured_mock):
        # Test that when max_sessions is reached, oldest sessions are evicted
        max_sessions = 3
        container = _StorageContainer(disable_isolation_mode=False, max_sessions=max_sessions)
        # Create sessions up to the limit
        storages = []
        for i in range(max_sessions):
            session_id = f"session_{i}"
            _, storage = container.get_storage(session_id)
            storages.append(storage)
            # Verify storage was created
            self.assertIsNotNone(storage)
            # Verify it's in the container
            self.assertIn(session_id, container.index_map)
            # Verify heap properties after each insertion
            self._verify_heap_property(container)
            self._verify_index_consistency(container)
        # All sessions should be present
        self.assertEqual(len(container.index_map), max_sessions)
        self.assertEqual(len(container.heap), max_sessions)
        # Add one more session - should evict the oldest (first) session
        new_session_id = "session_new"
        _, new_storage = container.get_storage(new_session_id)
        # Verify heap properties after eviction and insertion
        self._verify_heap_property(container)
        self._verify_index_consistency(container)
        # Should still have max_sessions total
        self.assertEqual(len(container.index_map), max_sessions)
        self.assertEqual(len(container.heap), max_sessions)
        # New session should be present
        self.assertIn(new_session_id, container.index_map)
        # First session should have been evicted (it had the smallest timestamp)
        self.assertNotIn("session_0", container.index_map)

    @patch("realworld_dummy_server.log_structured")
    def test_max_sessions_is_working_with_a_sequence_of_calls_actually_triggering_reorders(self, log_structured_mock):
        # Test that accessing existing sessions updates their priority and affects eviction order
        import time

        max_sessions = 3
        container = _StorageContainer(disable_isolation_mode=False, max_sessions=max_sessions)
        # Create initial sessions
        session_ids = ["session_1", "session_2", "session_3"]
        for session_id in session_ids:
            container.get_storage(session_id)
            time.sleep(0.00001)  # Small delay to ensure different timestamps
            # Verify heap properties after each insertion
            self._verify_heap_property(container)
            self._verify_index_consistency(container)
        # Access session_1 to update its priority (make it more recently used)
        time.sleep(0.00001)
        container.get_storage("session_1")
        self._verify_heap_property(container)
        self._verify_index_consistency(container)
        # Add a new session - should evict session_2 (oldest untouched)
        time.sleep(0.00001)
        container.get_storage("session_4")
        self._verify_heap_property(container)
        self._verify_index_consistency(container)
        # Verify session_1 and session_3 are still present (session_1 was recently accessed)
        self.assertIn("session_1", container.index_map)
        self.assertIn("session_3", container.index_map)
        self.assertIn("session_4", container.index_map)
        # session_2 should have been evicted (it was the oldest unused)
        self.assertNotIn("session_2", container.index_map)
        # Verify we still have exactly max_sessions
        self.assertEqual(len(container.index_map), max_sessions)
        self.assertEqual(len(container.heap), max_sessions)
        # Access session_3 multiple times to make it most recent
        time.sleep(0.00001)
        container.get_storage("session_3")
        self._verify_heap_property(container)
        self._verify_index_consistency(container)
        time.sleep(0.00001)
        container.get_storage("session_3")
        self._verify_heap_property(container)
        self._verify_index_consistency(container)
        # Add another session - should evict session_1 now (oldest of remaining)
        time.sleep(0.00001)
        container.get_storage("session_5")
        self._verify_heap_property(container)
        self._verify_index_consistency(container)
        # Verify session_3 is still present (most recently accessed)
        self.assertIn("session_3", container.index_map)
        self.assertIn("session_4", container.index_map)
        self.assertIn("session_5", container.index_map)
        # session_1 should now be evicted
        self.assertNotIn("session_1", container.index_map)

    # Tests - _handle_client_ip_and_session* methods

    def test_handle_client_ip_and_session_eviction_with_empty_ip(self):
        """Test eviction with empty/None client_ip"""
        self.container._push(1, "session1", "data1", "192.168.1.1")
        self.container.ip_to_sessions["192.168.1.1"] = ["session1"]
        self.container._handle_client_ip_and_session_eviction("session1", None)
        self.assertEqual(self.container.ip_to_sessions, {"192.168.1.1": ["session1"]})
        self.container._handle_client_ip_and_session_eviction("session1", "")
        self.assertEqual(self.container.ip_to_sessions, {"192.168.1.1": ["session1"]})

    def test_handle_client_ip_and_session_eviction_removes_session_from_ip(self):
        """Test eviction removes session from ip_to_sessions"""
        self.container._push(1, "session1", "data1", "192.168.1.1")
        self.container._push(2, "session2", "data2", "192.168.1.1")
        self.container.ip_to_sessions["192.168.1.1"] = ["session1", "session2"]
        self.container._handle_client_ip_and_session_eviction("session1", "192.168.1.1")
        self.assertEqual(self.container.ip_to_sessions, {"192.168.1.1": ["session2"]})

    def test_handle_client_ip_and_session_eviction_removes_empty_ip_entry(self):
        """Test eviction removes IP entry when it becomes empty"""
        self.container._push(1, "session1", "data1", "192.168.1.1")
        self.container.ip_to_sessions["192.168.1.1"] = ["session1"]
        self.container._handle_client_ip_and_session_eviction("session1", "192.168.1.1")
        self.assertEqual(self.container.ip_to_sessions, {})

    def test_handle_client_ip_and_session_eviction_nonexistent_ip(self):
        """Test eviction with non-existent IP"""
        self.container._handle_client_ip_and_session_eviction("session1", "192.168.1.1")
        self.assertEqual(self.container.ip_to_sessions, {})

    def test_handle_client_ip_and_session_eviction_ipv6_normalization(self):
        """Test eviction with IPv6 address normalization"""
        ipv6_addr = "2001:db8:85a3:8d3:1319:8a2e:370:7348"
        normalized_ip = "2001:db8:85a3:8d3::/64"
        self.container._push(1, "session1", "data1", ipv6_addr)
        self.container.ip_to_sessions[normalized_ip] = ["session1"]
        self.container._handle_client_ip_and_session_eviction("session1", ipv6_addr)
        self.assertEqual(self.container.ip_to_sessions, {})

    def test_handle_client_ip_and_session_addition_with_empty_ip(self):
        """Test addition with empty/None client_ip"""
        self.container._handle_client_ip_and_session_addition("session1", None)
        self.assertEqual(self.container.ip_to_sessions, {})
        self.container._handle_client_ip_and_session_addition("session1", "")
        self.assertEqual(self.container.ip_to_sessions, {})

    def test_handle_client_ip_and_session_addition_new_ip(self):
        """Test addition creates new IP entry"""
        self.container._handle_client_ip_and_session_addition("session1", "192.168.1.1")
        self.assertEqual(self.container.ip_to_sessions["192.168.1.1"], ["session1"])

    def test_handle_client_ip_and_session_addition_existing_ip(self):
        """Test addition to existing IP entry"""
        self.container.ip_to_sessions["192.168.1.1"] = ["session1"]
        self.container._handle_client_ip_and_session_addition("session2", "192.168.1.1")
        self.assertEqual(self.container.ip_to_sessions["192.168.1.1"], ["session1", "session2"])

    def test_handle_client_ip_and_session_addition_exceeds_max_sessions_by_one(self):
        """Test addition that exceeds MAX_SESSIONS_PER_IP by one"""
        # Setup sessions up to the limit
        sessions = [f"session{i}" for i in range(MAX_SESSIONS_PER_IP + 1)]
        for i, session in enumerate(sessions[:-1]):
            self.container._push(i, session, f"data{i}", "192.168.1.1")
        self.container.ip_to_sessions["192.168.1.1"] = sessions[:-1]
        # Add the final session that exceeds the limit
        final_session = sessions[-1]
        self.container._push(len(sessions), final_session, f"data{len(sessions)}", "192.168.1.1")
        # This should trigger eviction
        self.container._handle_client_ip_and_session_addition(final_session, "192.168.1.1")
        # Verify the session was added and the first session was deleted
        self.assertIn(final_session, self.container.ip_to_sessions["192.168.1.1"])
        self.assertEqual(self.container.ip_to_sessions, {"192.168.1.1": [f"session{i}" for i in range(1, 31)]})

    def test_handle_client_ip_and_session_addition_exceeds_max_sessions_by_multiple(self):
        """Test addition that exceeds MAX_SESSIONS_PER_IP by multiple"""
        # Setup sessions up to the limit
        sessions = [f"session{i}" for i in range(MAX_SESSIONS_PER_IP + 5)]
        for i, session in enumerate(sessions[:-1]):
            self.container._push(i, session, f"data{i}", "192.168.1.1")
        self.container.ip_to_sessions["192.168.1.1"] = sessions[:-1]
        # Add the final session that exceeds the limit
        final_session = sessions[-1]
        self.container._push(len(sessions), final_session, f"data{len(sessions)}", "192.168.1.1")
        # This should trigger eviction
        self.container._handle_client_ip_and_session_addition(final_session, "192.168.1.1")
        # Verify the session was added and the first session was deleted
        self.assertIn(final_session, self.container.ip_to_sessions["192.168.1.1"])
        self.assertEqual(self.container.ip_to_sessions, {"192.168.1.1": [f"session{i}" for i in range(5, 35)]})

    def test_handle_client_ip_and_session_addition_ipv6_normalization(self):
        """Test addition with IPv6 address normalization"""
        ipv6_addr = "2001:db8:85a3:8d3:1319:8a2e:370:7348"
        normalized_ip = "2001:db8:85a3:8d3::/64"
        self.container._handle_client_ip_and_session_addition("session1", ipv6_addr)
        self.assertEqual(self.container.ip_to_sessions, {normalized_ip: ["session1"]})

    def test_handle_client_ip_and_session_priority_with_empty_ip(self):
        """Test priority handling with empty/None client_ip"""
        self.container._push(1, "session1", "data1", "192.168.1.1")
        self.container._handle_client_ip_and_session_priority("session1", None)
        self.container._handle_client_ip_and_session_priority("session1", "")
        self.assertEqual(self.container.ip_to_sessions, {})

    def test_handle_client_ip_and_session_priority_same_ip_one_session(self):
        """Test priority handling when session IP hasn't changed"""
        self.container._push(1, "session1", "data1", "192.168.1.1")
        self.container.ip_to_sessions["192.168.1.1"] = ["session1"]
        self.container._handle_client_ip_and_session_priority("session1", "192.168.1.1")
        self.assertEqual(self.container.ip_to_sessions, {"192.168.1.1": ["session1"]})

    def test_handle_client_ip_and_session_priority_same_ip_no_reorder(self):
        """Test priority handling when session IP hasn't changed"""
        self.container._push(1, "session1", "data1", "192.168.1.1")
        self.container._push(2, "session2", "data2", "192.168.1.1")
        self.container.ip_to_sessions["192.168.1.1"] = ["session1", "session2"]
        self.container._handle_client_ip_and_session_priority("session2", "192.168.1.1")
        self.assertEqual(self.container.ip_to_sessions, {"192.168.1.1": ["session1", "session2"]})

    def test_handle_client_ip_and_session_priority_same_ip_with_reorder(self):
        """Test priority handling when session IP hasn't changed"""
        self.container._push(1, "session1", "data1", "192.168.1.1")
        self.container._push(2, "session2", "data2", "192.168.1.1")
        self.container.ip_to_sessions["192.168.1.1"] = ["session1", "session2"]
        self.container._handle_client_ip_and_session_priority("session1", "192.168.1.1")
        self.assertEqual(self.container.ip_to_sessions, {"192.168.1.1": ["session2", "session1"]})

    def test_handle_client_ip_and_session_priority_ip_not_in_sessions_and_no_sessions(self):
        """Test priority handling when IP not in sessions (edge case) - no sessions at all"""
        self.container._push(1, "session1", "data1", "192.168.1.1")
        self.container._handle_client_ip_and_session_priority("session1", "192.168.1.1")
        self.assertEqual(self.container.ip_to_sessions, {"192.168.1.1": ["session1"]})

    def test_handle_client_ip_and_session_priority_ip_not_in_sessions_and_existing_session(self):
        """Test priority handling when IP not in sessions (edge case) - existing session for that ip"""
        self.container._push(1, "session1", "data1", "192.168.1.1")
        self.container._push(2, "session2", "data2", "192.168.1.1")
        self.container.ip_to_sessions["192.168.1.1"] = ["session2"]
        self.container._handle_client_ip_and_session_priority("session1", "192.168.1.1")
        self.assertEqual(self.container.ip_to_sessions, {"192.168.1.1": ["session2", "session1"]})

    def test_handle_client_ip_and_session_priority_different_ip_only_one_session(self):
        """Test priority handling when session IP has changed - only one session exists"""
        self.container._push(1, "session1", "data1", "192.168.1.1")
        self.container._handle_client_ip_and_session_priority("session1", "192.168.1.2")
        self.assertEqual(self.container.ip_to_sessions, {"192.168.1.2": ["session1"]})

    def test_handle_client_ip_and_session_priority_different_ip_one_other_session_for_previous_ip(self):
        """Test priority handling when session IP has changed - one other session for previous ip"""
        self.container._push(0, "session0", "data0", "192.168.1.1")
        self.container._push(1, "session1", "data1", "192.168.1.1")
        self.container.ip_to_sessions["192.168.1.1"] = ["session0", "session1"]
        self.container._handle_client_ip_and_session_priority("session1", "192.168.1.2")
        self.assertEqual(self.container.ip_to_sessions, {"192.168.1.1": ["session0"], "192.168.1.2": ["session1"]})

    def test_handle_client_ip_and_session_priority_different_ip_one_other_session_for_next_ip(self):
        """Test priority handling when session IP has changed - one other session for next ip"""
        self.container._push(0, "session0", "data0", "192.168.1.2")
        self.container._push(1, "session1", "data1", "192.168.1.1")
        self.container.ip_to_sessions["192.168.1.2"] = ["session0"]
        self.container.ip_to_sessions["192.168.1.1"] = ["session1"]
        self.container._handle_client_ip_and_session_priority("session1", "192.168.1.2")
        self.assertEqual(self.container.ip_to_sessions, {"192.168.1.2": ["session0", "session1"]})

    def test_handle_client_ip_and_session_priority_different_ip_max_other_session_for_next_ip(self):
        """Test priority handling when session IP has changed - enough sessions for next ip to trigger cleaning"""
        self.container._push(0, "session0", "data0", "192.168.1.1")
        self.container.ip_to_sessions["192.168.1.1"] = ["session0"]
        self.container.ip_to_sessions["192.168.1.2"] = []
        for i in range(1, MAX_SESSIONS_PER_IP + 1):
            self.container._push(i, f"session{i}", f"data{i}", "192.168.1.2")
            self.container.ip_to_sessions["192.168.1.2"].append(f"session{i}")
        self.container._handle_client_ip_and_session_priority("session0", "192.168.1.2")
        self.assertEqual(
            self.container.ip_to_sessions,
            {"192.168.1.2": [*(f"session{i}" for i in range(2, MAX_SESSIONS_PER_IP + 1)), "session0"]},
        )

    def test_handle_client_ip_and_session_priority_different_ip_more_than_max_other_session_for_next_ip(self):
        """Test priority handling when session IP has changed - enough sessions for next ip to trigger cleaning x5"""
        self.container._push(0, "session0", "data0", "192.168.1.1")
        self.container.ip_to_sessions["192.168.1.1"] = ["session0"]
        self.container.ip_to_sessions["192.168.1.2"] = []
        for i in range(1, MAX_SESSIONS_PER_IP + 5):
            self.container._push(i, f"session{i}", f"data{i}", "192.168.1.2")
            self.container.ip_to_sessions["192.168.1.2"].append(f"session{i}")
        self.container._handle_client_ip_and_session_priority("session0", "192.168.1.2")
        self.assertEqual(
            self.container.ip_to_sessions,
            {"192.168.1.2": [*(f"session{i}" for i in range(6, MAX_SESSIONS_PER_IP + 5)), "session0"]},
        )

    def test_handle_client_ip_and_session_reattribution_updates_data_structure(self):
        """Test reattribution updates heap data structure"""
        self.container._push(1, "session1", "data1", "192.168.1.1")
        self.container._handle_client_ip_and_session_reattribution("session1", "192.168.1.1", "192.168.1.2")
        session_data = self.container.heap[self.container.index_map["session1"]]
        self.assertEqual(session_data[4], "192.168.1.2")
        self.assertEqual(self.container.ip_to_sessions, {"192.168.1.2": ["session1"]})

    def test_handle_client_ip_and_session_reattribution_with_none_old_ip(self):
        """Test reattribution with None old IP"""
        self.container._push(1, "session1", "data1", "192.168.1.1")
        self.container._handle_client_ip_and_session_reattribution("session1", None, "192.168.1.2")
        self.assertEqual(self.container.ip_to_sessions, {"192.168.1.2": ["session1"]})

    def test_normalize_ip_for_limiting_ipv4(self):
        """Test IP normalization for IPv4 addresses"""
        result = self.container._normalize_ip_for_limiting("192.168.1.1")
        self.assertEqual(result, "192.168.1.1")

    def test_normalize_ip_for_limiting_ipv6(self):
        """Test IP normalization for IPv6 addresses"""
        ipv6_addr = "2001:db8:85a3:8d3:1319:8a2e:370:7348"
        result = self.container._normalize_ip_for_limiting(ipv6_addr)
        self.assertEqual(result, "2001:db8:85a3:8d3::/64")

    def test_normalize_ip_for_limiting_ipv6_short(self):
        """Test IP normalization for short IPv6 addresses"""
        ipv6_addr = "2001:db8:85a3"
        result = self.container._normalize_ip_for_limiting(ipv6_addr)
        self.assertEqual(result, "2001:db8:85a3/64")

    def test_normalize_ip_for_limiting_already_normalized(self):
        """Test IP normalization for already normalized IPv6"""
        ipv6_normalized = "2001:db8:85a3:8d3::/64"
        result = self.container._normalize_ip_for_limiting(ipv6_normalized)
        self.assertEqual(result, ipv6_normalized)

    # Tests - get_storage

    def test_get_storage_with_isolation_disabled(self):
        container = _StorageContainer(disable_isolation_mode=True)
        id0, storage0 = container.get_storage(None)
        id1, storage1 = container.get_storage("session1")
        id2, storage2 = container.get_storage("session2")
        # All should return the same storage
        self.assertIs(storage1, storage0)
        self.assertIs(storage2, storage0)
        # Check returned IDs
        self.assertIsNone(id0)
        self.assertIsNone(id1)
        self.assertIsNone(id2)

    @patch("realworld_dummy_server.log_structured")
    def test_get_storage_with_isolation_enabled_2_different_session(self, log_structured_mock):
        container = _StorageContainer(disable_isolation_mode=False)
        id1, storage1 = container.get_storage("session1")
        id2, storage2 = container.get_storage("session2")
        # Different sessions should get different storage
        self.assertIsNot(storage1, storage2)
        # Check returned IDs
        self.assertEqual(id1, "session1")
        self.assertEqual(id2, "session2")

    @patch("realworld_dummy_server.log_structured")
    def test_get_storage_with_isolation_enabled_2_same(self, log_structured_mock):
        container = _StorageContainer(disable_isolation_mode=False)
        id1, storage1 = container.get_storage("session1")
        container.get_storage("something-else")  # Call to other session in between
        id1_bis, storage1_bis = container.get_storage("session1")
        # Storage containers from the same id should get the same storage
        self.assertIs(storage1, storage1_bis)
        # Check returned IDs
        self.assertEqual(id1, "session1")
        self.assertEqual(id1_bis, "session1")

    @patch("realworld_dummy_server.log_structured")
    def test_get_storage_with_isolation_enabled_2_default_sessions_are_not_the_same_none_version(
        self, log_structured_mock
    ):
        """We don't want the modifications of a default session to have an impact for other users"""
        container = _StorageContainer(disable_isolation_mode=False)
        id1, storage1 = container.get_storage(None)
        id2, storage2 = container.get_storage(None)
        # Multiple defaults sessions should get different storage
        self.assertIsNot(storage1, storage2)
        # Check returned IDs - they should be different UUIDs
        self.assertIsNotNone(id1)
        self.assertIsNotNone(id2)
        self.assertNotEqual(id1, id2)

    @patch("realworld_dummy_server.log_structured")
    def test_get_storage_with_isolation_enabled_2_default_sessions_are_not_the_same_empty_string_version(
        self, log_structured_mock
    ):
        """We don't want the modifications of a default session to have an impact for other users"""
        container = _StorageContainer(disable_isolation_mode=False)
        id1, storage1 = container.get_storage("")
        id2, storage2 = container.get_storage("")
        # Multiple defaults sessions should get different storage
        self.assertIsNot(storage1, storage2)
        # Check returned IDs - they should be different UUIDs
        self.assertIsNotNone(id1)
        self.assertIsNotNone(id2)
        self.assertNotEqual(id1, id2)

    @patch("realworld_dummy_server.log_structured")
    def test_get_storage_with_jwt_token_existing_session(self, log_structured_mock):
        """Test get_storage with JWT token that maps to existing session"""
        container = _StorageContainer(disable_isolation_mode=False)
        # First create a session
        session_id, storage = container.get_storage("test_session")
        # Bind JWT to session
        container.jwt_to_session["test_jwt"] = session_id
        container.jwt_to_session_order.append("test_jwt")
        # Now get storage using JWT token
        returned_session_id, returned_storage = container.get_storage(None, jwt_token="test_jwt")
        # Should return the same session and storage
        self.assertEqual(returned_session_id, session_id)
        self.assertIs(returned_storage, storage)
        # JWT should be moved to end of order list
        self.assertEqual(container.jwt_to_session_order[-1], "test_jwt")

    @patch("realworld_dummy_server.log_structured")
    def test_get_storage_with_jwt_token_nonexistent_session(self, log_structured_mock):
        """Test get_storage with JWT token that maps to nonexistent session"""
        container = _StorageContainer(disable_isolation_mode=False)
        # Bind JWT to nonexistent session
        container.jwt_to_session["test_jwt"] = "nonexistent_session"
        container.jwt_to_session_order.append("test_jwt")
        # Get storage using JWT token
        session_id, storage = container.get_storage(None, jwt_token="test_jwt")
        # Should create new session since mapped session doesn't exist
        self.assertIsNotNone(session_id)
        self.assertIsInstance(storage, InMemoryStorage)

    @patch("realworld_dummy_server.log_structured")
    def test_get_storage_with_jwt_token_no_mapping(self, log_structured_mock):
        """Test get_storage with JWT token that has no session mapping"""
        container = _StorageContainer(disable_isolation_mode=False)
        # Get storage using unmapped JWT token
        session_id, storage = container.get_storage(None, jwt_token="unmapped_jwt")
        # Should create new session
        self.assertIsNotNone(session_id)
        self.assertIsInstance(storage, InMemoryStorage)

    @patch("realworld_dummy_server.log_structured")
    def test_get_storage_cookie_priority_over_jwt(self, log_structured_mock):
        """Test that session cookie takes priority over JWT token"""
        container = _StorageContainer(disable_isolation_mode=False)
        # Create session via cookie
        cookie_session_id, cookie_storage = container.get_storage("cookie_session")
        # Create different session and bind JWT to it
        jwt_session_id, jwt_storage = container.get_storage("jwt_session")
        container.jwt_to_session["test_jwt"] = jwt_session_id
        container.jwt_to_session_order.append("test_jwt")
        # Get storage with both cookie and JWT
        returned_session_id, returned_storage = container.get_storage("cookie_session", jwt_token="test_jwt")
        # Should return cookie session, not JWT session
        self.assertEqual(returned_session_id, cookie_session_id)
        self.assertIs(returned_storage, cookie_storage)

    @patch("realworld_dummy_server.log_structured")
    def test_get_storage_max_sessions_eviction(self, log_structured_mock):
        """Test that max sessions limit triggers eviction"""
        container = _StorageContainer(disable_isolation_mode=False, max_sessions=2)
        # Create max sessions
        container.get_storage("session1")
        id_2, storage_2 = container.get_storage("session2")
        # Creating third session should trigger eviction
        id_3, storage_3 = container.get_storage("session3")
        # Should have exactly max_sessions entries
        self.assertEqual(len(container.index_map), 2)
        self.assertIsNotNone(id_3)
        self.assertIsInstance(storage_3, InMemoryStorage)
        # id_2 and id_3 stored
        self.assertEqual({id_2, id_3}, set(container.index_map))

    # Tests - bind_jwt_to_session_id

    def test_bind_jwt_to_session_id_with_isolation_disabled(self):
        """Test bind_jwt_to_session_id when isolation mode is disabled"""
        container = _StorageContainer(disable_isolation_mode=True)
        # Should do nothing when isolation disabled
        container.bind_jwt_to_session_id("test_jwt", "test_session")
        # JWT mappings should remain empty
        self.assertEqual(len(container.jwt_to_session), 0)
        self.assertEqual(len(container.jwt_to_session_order), 0)

    def test_bind_jwt_to_session_id_new_binding(self):
        """Test binding new JWT token to session"""
        container = _StorageContainer(disable_isolation_mode=False)
        # Bind JWT to session
        container.bind_jwt_to_session_id("test_jwt", "test_session")
        # JWT should be bound to session
        self.assertEqual(container.jwt_to_session["test_jwt"], "test_session")
        self.assertIn("test_jwt", container.jwt_to_session_order)

    def test_bind_jwt_to_session_id_replace_existing_jwt(self):
        """Test replacing existing JWT token mapping"""
        container = _StorageContainer(disable_isolation_mode=False)
        # Create initial binding
        container.jwt_to_session["test_jwt"] = "old_session"
        container.jwt_to_session_order.append("test_jwt")
        # Replace binding
        container.bind_jwt_to_session_id("test_jwt", "new_session")
        # JWT should be bound to new session
        self.assertEqual(container.jwt_to_session["test_jwt"], "new_session")
        self.assertIn("test_jwt", container.jwt_to_session_order)

    def test_bind_jwt_to_session_id_replace_session_mapping(self):
        """Test replacing session that already has JWT mapping"""
        container = _StorageContainer(disable_isolation_mode=False)
        # Create initial binding
        container.jwt_to_session["old_jwt"] = "test_session"
        container.jwt_to_session_order.append("old_jwt")
        # Bind new JWT to same session
        container.bind_jwt_to_session_id("new_jwt", "test_session")
        # Old JWT should be removed, new JWT should be bound
        self.assertNotIn("old_jwt", container.jwt_to_session)
        self.assertEqual(container.jwt_to_session["new_jwt"], "test_session")
        self.assertNotIn("old_jwt", container.jwt_to_session_order)
        self.assertIn("new_jwt", container.jwt_to_session_order)

    def test_bind_jwt_to_session_id_max_sessions_eviction(self):
        """Test that max sessions limit triggers JWT eviction"""
        container = _StorageContainer(disable_isolation_mode=False, max_sessions=4)
        # Fill up to max sessions
        for i in range(4):
            container.jwt_to_session[f"jwt_{i}"] = f"session_{i}"
            container.jwt_to_session_order.append(f"jwt_{i}")
        # Bind new JWT should evict oldest
        container.bind_jwt_to_session_id("new_jwt", "new_session")
        # Oldest JWT should be removed
        self.assertNotIn("jwt_0", container.jwt_to_session)
        self.assertNotIn("jwt_0", container.jwt_to_session_order)
        # New JWT should be bound
        self.assertEqual(container.jwt_to_session["new_jwt"], "new_session")
        self.assertIn("new_jwt", container.jwt_to_session_order)

    def test_find_session_by_credentials_not_found(self):
        """Test finding session with credentials that don't exist"""
        container = _StorageContainer(disable_isolation_mode=False)
        storage = InMemoryStorage()
        container.push(1, "session_1", data=storage, client_ip=None)
        # Search for non-existent credentials
        found_session, found_storage = container.find_session_by_credentials(
            "nonexistent@example.com", "hashed_password"
        )
        self.assertIsNone(found_session)
        self.assertIsNone(found_storage)

    def test_find_session_by_credentials_found_in_first_session(self):
        """Test finding session with matching credentials in first session"""
        container = _StorageContainer(disable_isolation_mode=False)
        storage = InMemoryStorage()
        # Add user to storage
        storage.users.add(
            {
                "email": "test@example.com",
                "username": "testuser",
                "password": "hashed_password_123",
                "bio": "",
                "image": "",
            }
        )
        container.push(1, "session_1", data=storage, client_ip=None)
        # Search for the user
        found_session, found_storage = container.find_session_by_credentials("test@example.com", "hashed_password_123")
        self.assertEqual(found_session, "session_1")
        self.assertIs(found_storage, storage)

    def test_find_session_by_credentials_found_in_later_session(self):
        """Test finding session with matching credentials in non-first session"""
        container = _StorageContainer(disable_isolation_mode=False)
        # Create first storage without the user
        storage1 = InMemoryStorage()
        storage1.users.add(
            {
                "email": "other@example.com",
                "username": "otheruser",
                "password": "other_password",
                "bio": "",
                "image": "",
            }
        )
        container.push(1, "session_1", data=storage1, client_ip=None)
        # Create second storage with the target user
        storage2 = InMemoryStorage()
        storage2.users.add(
            {
                "email": "target@example.com",
                "username": "targetuser",
                "password": "hashed_password_456",
                "bio": "",
                "image": "",
            }
        )
        container.push(2, "session_2", data=storage2, client_ip=None)
        # Search for the target user
        found_session, found_storage = container.find_session_by_credentials(
            "target@example.com", "hashed_password_456"
        )
        self.assertEqual(found_session, "session_2")
        self.assertIs(found_storage, storage2)

    def test_find_session_by_credentials_wrong_password(self):
        """Test that wrong password returns None"""
        container = _StorageContainer(disable_isolation_mode=False)
        storage = InMemoryStorage()
        storage.users.add(
            {
                "email": "test@example.com",
                "username": "testuser",
                "password": "correct_password",
                "bio": "",
                "image": "",
            }
        )
        container.push(1, "session_1", data=storage, client_ip=None)
        # Search with wrong password
        found_session, found_storage = container.find_session_by_credentials("test@example.com", "wrong_password")
        self.assertIsNone(found_session)
        self.assertIsNone(found_storage)

    def test_find_session_by_credentials_with_isolation_disabled(self):
        """Test that method returns None when isolation is disabled"""
        container = _StorageContainer(disable_isolation_mode=True)
        # Even with data in heap, should return None when isolation disabled
        found_session, found_storage = container.find_session_by_credentials("any@example.com", "any_password")
        self.assertIsNone(found_session)
        self.assertIsNone(found_storage)

    def test_find_session_by_credentials_with_multiple_users_in_session(self):
        """Test finding correct user when multiple users exist in a session"""
        container = _StorageContainer(disable_isolation_mode=False)
        storage = InMemoryStorage()
        # Add multiple users
        storage.users.add(
            {"email": "user1@example.com", "username": "user1", "password": "password1", "bio": "", "image": ""}
        )
        storage.users.add(
            {"email": "user2@example.com", "username": "user2", "password": "password2", "bio": "", "image": ""}
        )
        storage.users.add(
            {"email": "target@example.com", "username": "target", "password": "target_password", "bio": "", "image": ""}
        )
        container.push(1, "session_1", data=storage, client_ip=None)
        # Search for specific user
        found_session, found_storage = container.find_session_by_credentials("target@example.com", "target_password")
        self.assertEqual(found_session, "session_1")
        self.assertIs(found_storage, storage)

    def test_multiple_sessions_share_storage_survives_partial_eviction(self):
        """
        Test that storage persists while ANY session references it, even after some sessions are evicted.
        Scenario:
        1. Create session_a with storage containing user data
        2. Create session_b pointing to same storage (simulating login on another device)
        3. Fill up sessions to trigger evictions
        4. Access session_a repeatedly to keep it alive
        5. Verify session_b gets evicted (not accessed, lower priority)
        6. Verify session_a still has access to the shared storage
        7. Fill more sessions until session_a is also evicted
        8. Verify storage is no longer accessible (implicitly garbage collected)
        """
        # Set max sessions to 3 to make evictions happen quickly
        container = _StorageContainer(disable_isolation_mode=False, max_sessions=3)
        # Step 1: Create session_a with storage containing user data
        storage_shared = InMemoryStorage()
        storage_shared.users.add(
            {
                "email": "shared@example.com",
                "username": "shareduser",
                "password": "hashed_password",
                "bio": "Shared storage user",
                "image": "",
            }
        )
        container.push(100, "session_a", data=storage_shared, client_ip=None)
        # Step 2: Create session_b pointing to SAME storage (simulating login on device B)
        container.push(200, "session_b", data=storage_shared, client_ip=None)
        # Verify both sessions see the same storage object
        self.assertIs(container.heap[container.index_map["session_a"]][2], storage_shared)
        self.assertIs(container.heap[container.index_map["session_b"]][2], storage_shared)
        # Step 3: Create session_c with different storage (fills max_sessions=3)
        storage_c = InMemoryStorage()
        container.push(300, "session_c", data=storage_c, client_ip=None)
        self.assertEqual(len(container.heap), 3)
        self.assertIn("session_a", container.index_map)
        self.assertIn("session_b", container.index_map)
        self.assertIn("session_c", container.index_map)
        # Step 4: Access session_a to update its priority (simulate active use)
        container.update_priority("session_a", 1000, client_ip=None)
        # Step 5: Create session_d - need to manually evict first (simulating get_storage behavior)
        # At max_sessions, so pop the lowest priority item first
        evicted = container.pop()
        self.assertEqual(evicted[1], "session_b")  # session_b has priority 200 (lowest)
        storage_d = InMemoryStorage()
        container.push(400, "session_d", data=storage_d, client_ip=None)
        # Verify session_b was evicted but session_a survives
        self.assertNotIn("session_b", container.index_map)
        self.assertIn("session_a", container.index_map)
        # Step 6: Access session_a again to keep it alive
        container.update_priority("session_a", 2000, client_ip=None)
        # Step 7: Create session_e - manually evict lowest priority first
        # Current: session_c (300), session_d (400), session_a (2000)
        evicted = container.pop()
        self.assertEqual(evicted[1], "session_c")  # session_c has lowest priority (300)
        storage_e = InMemoryStorage()
        container.push(500, "session_e", data=storage_e, client_ip=None)
        # Verify session_a STILL survives and has access to shared storage
        self.assertIn("session_a", container.index_map)
        session_a_storage = container.heap[container.index_map["session_a"]][2]
        self.assertIs(session_a_storage, storage_shared)
        # Verify the user data is still accessible through session_a
        retrieved_user = next((u for u in session_a_storage.users.values() if u["email"] == "shared@example.com"), None)
        self.assertIsNotNone(retrieved_user)
        self.assertEqual(retrieved_user["username"], "shareduser")
        # Step 8: Create more sessions to eventually evict session_a
        # Current: session_d (400), session_e (500), session_a (2000)
        evicted = container.pop()
        self.assertEqual(evicted[1], "session_d")  # Lowest priority 400
        storage_f = InMemoryStorage()
        container.push(3000, "session_f", data=storage_f, client_ip=None)
        # session_a still alive
        self.assertIn("session_a", container.index_map)
        self.assertNotIn("session_d", container.index_map)
        # Current: session_e (500), session_a (2000), session_f (3000)
        evicted = container.pop()
        self.assertEqual(evicted[1], "session_e")  # Lowest priority 500
        storage_g = InMemoryStorage()
        container.push(4000, "session_g", data=storage_g, client_ip=None)
        # session_a still alive!
        self.assertIn("session_a", container.index_map)
        self.assertNotIn("session_e", container.index_map)
        # Current: session_a (2000), session_f (3000), session_g (4000)
        evicted = container.pop()
        self.assertEqual(evicted[1], "session_a")  # Finally, session_a gets evicted (lowest priority 2000)
        storage_h = InMemoryStorage()
        container.push(5000, "session_h", data=storage_h, client_ip=None)
        # session_a is now gone
        self.assertNotIn("session_a", container.index_map)
        # Step 9: Verify storage_shared is no longer in the heap
        # (Python will garbage collect it since no references remain)
        for heap_item in container.heap:
            _, _, storage, _, _ = heap_item
            self.assertIsNot(storage, storage_shared, "Shared storage should not be in any remaining session")


class TestSaveAndLoadData(TestCase):
    TEST_DATA_FILE_PATH = Path("test-file-save-data-b29e89dd-d67a-4ef6-ab2d-09d6204771bf")
    TEST_DATA_EXPECTED_FILE_CONTENT = {
        "session_2": {
            "users": {
                "objects": {
                    "1": {
                        "email": "user3@example.com",
                        "username": "user3",
                        "password": "hashed_password_3",
                        "bio": "Bio for user 3 in session 2",
                        "image": "https://example.com/user3.jpg",
                        "id": "1",
                    },
                    "2": {
                        "email": "user4@example.com",
                        "username": "user4",
                        "password": "hashed_password_4",
                        "bio": "Bio for user 4 in session 2",
                        "image": "https://example.com/user4.jpg",
                        "id": "2",
                    },
                },
                **{"last_accessed_ids": ["1", "2"]},
                "current_id_counter": 3,
            },
            "articles": {
                "objects": {
                    "1": {
                        "title": "Third Article in Session 2",
                        "description": "This article is in a different session",
                        "body": "Content for the third article in session 2",
                        **{"tagList": ["session2", "testing"]},
                        "author": "1",
                        "slug": "third-article-session2",
                        "id": "1",
                    }
                },
                **{"last_accessed_ids": ["1"]},
                "current_id_counter": 2,
            },
            "comments": {
                **{"objects": {"1": {"body": "Comment from session 2", "author": "2", "article": "1", "id": "1"}}},
                **{"last_accessed_ids": ["1"]},
                "current_id_counter": 2,
            },
            **{"follows": [["1", "2"]]},
            **{"favorites": [["2", "1"]]},
        },
        "session_3": {
            "users": {
                "objects": {
                    "1": {
                        "email": "user5@example.com",
                        "username": "user5",
                        "password": "hashed_password_5",
                        "bio": "User 5 bio in session 3",
                        "image": "https://example.com/user5.jpg",
                        "id": "1",
                    },
                    "2": {
                        "email": "user6@example.com",
                        "username": "user6",
                        "password": "hashed_password_6",
                        "bio": "User 6 bio in session 3",
                        "image": "https://example.com/user6.jpg",
                        "id": "2",
                    },
                    "3": {
                        "email": "user7@example.com",
                        "username": "user7",
                        "password": "hashed_password_7",
                        "bio": "User 7 bio in session 3",
                        "image": "https://example.com/user7.jpg",
                        "id": "3",
                    },
                },
                **{"last_accessed_ids": ["1", "2", "3"]},
                "current_id_counter": 4,
            },
            "articles": {
                "objects": {
                    "1": {
                        "title": "Fourth Article Session 3",
                        "description": "Article 4 description",
                        "body": "Content for article 4 in session 3",
                        "tagList": ["session3", "multiple", "tags"],
                        "author": "1",
                        "slug": "fourth-article-session3",
                        "id": "1",
                    },
                    "2": {
                        "title": "Fifth Article Session 3",
                        "description": "Article 5 description",
                        "body": "Content for article 5 in session 3",
                        "tagList": ["more", "tags"],
                        "author": "2",
                        "slug": "fifth-article-session3",
                        "id": "2",
                    },
                },
                "last_accessed_ids": ["1", "2"],
                "current_id_counter": 3,
            },
            "comments": {
                "objects": {
                    "1": {"body": "First comment in session 3", "author": "2", "article": "1", "id": "1"},
                    "2": {"body": "Second comment in session 3", "author": "3", "article": "1", "id": "2"},
                    "3": {"body": "Third comment in session 3", "author": "1", "article": "2", "id": "3"},
                },
                **{"last_accessed_ids": ["1", "2", "3"]},
                "current_id_counter": 4,
            },
            **{"follows": [["1", "2"], ["2", "3"], ["3", "1"]]},
            **{"favorites": [["1", "2"], ["2", "1"], ["3", "1"], ["3", "2"]]},
        },
        "session_1": {
            "users": {
                "objects": {
                    "1": {
                        "email": "user1@example.com",
                        "username": "user1",
                        "password": "hashed_password_1",
                        "bio": "Bio for user 1",
                        "image": "https://example.com/user1.jpg",
                        "id": "1",
                    },
                    "2": {
                        "email": "user2@example.com",
                        "username": "user2",
                        "password": "hashed_password_2",
                        "bio": "Bio for user 2",
                        "image": "https://example.com/user2.jpg",
                        "id": "2",
                    },
                },
                **{"last_accessed_ids": ["2", "1"]},
                "current_id_counter": 3,
            },
            "articles": {
                "objects": {
                    "1": {
                        "title": "First Article",
                        "description": "Description of first article",
                        "body": "Body content of the first article with lots of text",
                        **{"tagList": ["tech", "programming"]},
                        "author": "1",
                        "slug": "first-article",
                        "id": "1",
                    },
                    "2": {
                        "title": "Second Article",
                        "description": "Description of second article",
                        "body": "Body content of the second article",
                        **{"tagList": ["science", "research"]},
                        "author": "2",
                        "slug": "second-article",
                        "id": "2",
                    },
                },
                **{"last_accessed_ids": ["2", "1"]},
                "current_id_counter": 3,
            },
            "comments": {
                "objects": {
                    **{"1": {"body": "Great article! Very informative.", "author": "2", "article": "1", "id": "1"}},
                    **{"2": {"body": "I disagree but gg.", "author": "1", "article": "1", "id": "2"}},
                },
                **{"last_accessed_ids": ["2", "1"]},
                "current_id_counter": 3,
            },
            **{"follows": [["1", "2"]]},
            **{"favorites": [["1", "2"], ["2", "1"]]},
        },
    }

    def setUp(self):
        # Set up a test file path
        global DATA_FILE_PATH
        self.TEST_DATA_FILE_PATH.unlink(missing_ok=True)
        self.original_data_file_path = DATA_FILE_PATH
        DATA_FILE_PATH = self.TEST_DATA_FILE_PATH
        # Clear the storage container
        global storage_container
        storage_container = _StorageContainer()

    def tearDown(self):
        # Restore original DATA_FILE_PATH
        global DATA_FILE_PATH
        DATA_FILE_PATH = self.original_data_file_path
        self.TEST_DATA_FILE_PATH.unlink(missing_ok=True)

    @patch("realworld_dummy_server.log_structured")
    def test_save_data_complex(self, log_structured_mock):
        """Complex test for save_data with multiple storages containing comprehensive data"""
        global storage_container
        # Create multiple storage sessions with comprehensive data
        _, storage1 = storage_container.get_storage("session_1")
        _, storage2 = storage_container.get_storage("session_2")
        _, storage3 = storage_container.get_storage("session_3")
        # Populate storage1 with multiple users
        user1_data = {
            "email": "user1@example.com",
            "username": "user1",
            "password": "hashed_password_1",
            "bio": "Bio for user 1",
            "image": "https://example.com/user1.jpg",
        }
        user2_data = {
            "email": "user2@example.com",
            "username": "user2",
            "password": "hashed_password_2",
            "bio": "Bio for user 2",
            "image": "https://example.com/user2.jpg",
        }
        user1, user2 = storage1.users.add(user1_data), storage1.users.add(user2_data)
        storage1.users.get(user1["id"])  # reorders data
        # Add articles to storage1
        article1_data = {
            "title": "First Article",
            "description": "Description of first article",
            "body": "Body content of the first article with lots of text",
            "tagList": ["tech", "programming"],
            "author": user1["id"],
            "slug": "first-article",
        }
        article2_data = {
            "title": "Second Article",
            "description": "Description of second article",
            "body": "Body content of the second article",
            "tagList": ["science", "research"],
            "author": user2["id"],
            "slug": "second-article",
        }
        article1, article2 = storage1.articles.add(article1_data), storage1.articles.add(article2_data)
        storage1.articles.get(article1["id"])  # reorders data
        # Add comments to storage1
        comment1_data = {"body": "Great article! Very informative.", "author": user2["id"], "article": article1["id"]}
        comment2_data = {"body": "I disagree but gg.", "author": user1["id"], "article": article1["id"]}
        comment1 = storage1.comments.add(comment1_data)
        storage1.comments.add(comment2_data)
        storage1.comments.get(comment1["id"])  # reorders data
        # Add follows and favorites to storage1
        storage1.follows.add(user1["id"], user2["id"])  # user1 follows user2
        storage1.favorites.add(user1["id"], article2["id"])  # user1 favorites article2
        storage1.favorites.add(user2["id"], article1["id"])  # user2 favorites article1
        # Populate storage2 with different data
        user3_data = {
            "email": "user3@example.com",
            "username": "user3",
            "password": "hashed_password_3",
            "bio": "Bio for user 3 in session 2",
            "image": "https://example.com/user3.jpg",
        }
        user4_data = {
            "email": "user4@example.com",
            "username": "user4",
            "password": "hashed_password_4",
            "bio": "Bio for user 4 in session 2",
            "image": "https://example.com/user4.jpg",
        }
        user3 = storage2.users.add(user3_data)
        user4 = storage2.users.add(user4_data)
        # Add articles to storage2
        article3_data = {
            "title": "Third Article in Session 2",
            "description": "This article is in a different session",
            "body": "Content for the third article in session 2",
            "tagList": ["session2", "testing"],
            "author": user3["id"],
            "slug": "third-article-session2",
        }
        article3 = storage2.articles.add(article3_data)
        # Add comments and links to storage2
        comment3_data = {"body": "Comment from session 2", "author": user4["id"], "article": article3["id"]}
        storage2.comments.add(comment3_data)
        storage2.follows.add(user3["id"], user4["id"])
        storage2.favorites.add(user4["id"], article3["id"])
        # Populate storage3 with even more data
        user5_data = {
            "email": "user5@example.com",
            "username": "user5",
            "password": "hashed_password_5",
            "bio": "User 5 bio in session 3",
            "image": "https://example.com/user5.jpg",
        }
        user6_data = {
            "email": "user6@example.com",
            "username": "user6",
            "password": "hashed_password_6",
            "bio": "User 6 bio in session 3",
            "image": "https://example.com/user6.jpg",
        }
        user7_data = {
            "email": "user7@example.com",
            "username": "user7",
            "password": "hashed_password_7",
            "bio": "User 7 bio in session 3",
            "image": "https://example.com/user7.jpg",
        }
        user5 = storage3.users.add(user5_data)
        user6 = storage3.users.add(user6_data)
        user7 = storage3.users.add(user7_data)
        # Add multiple articles to storage3
        article4_data = {
            "title": "Fourth Article Session 3",
            "description": "Article 4 description",
            "body": "Content for article 4 in session 3",
            "tagList": ["session3", "multiple", "tags"],
            "author": user5["id"],
            "slug": "fourth-article-session3",
        }
        article5_data = {
            "title": "Fifth Article Session 3",
            "description": "Article 5 description",
            "body": "Content for article 5 in session 3",
            "tagList": ["more", "tags"],
            "author": user6["id"],
            "slug": "fifth-article-session3",
        }
        article4 = storage3.articles.add(article4_data)
        article5 = storage3.articles.add(article5_data)
        # Add multiple comments to storage3
        comment4_data = {"body": "First comment in session 3", "author": user6["id"], "article": article4["id"]}
        comment5_data = {"body": "Second comment in session 3", "author": user7["id"], "article": article4["id"]}
        comment6_data = {"body": "Third comment in session 3", "author": user5["id"], "article": article5["id"]}
        storage3.comments.add(comment4_data)
        storage3.comments.add(comment5_data)
        storage3.comments.add(comment6_data)
        # Add complex follow/favorite relationships in storage3
        storage3.follows.add(user5["id"], user6["id"])  # user5 follows user6
        storage3.follows.add(user6["id"], user7["id"])  # user6 follows user7
        storage3.follows.add(user7["id"], user5["id"])  # user7 follows user5 (circular)
        storage3.favorites.add(user5["id"], article5["id"])  # user5 favorites article5
        storage3.favorites.add(user6["id"], article4["id"])  # user6 favorites article4
        storage3.favorites.add(user7["id"], article4["id"])  # user7 favorites article4
        storage3.favorites.add(user7["id"], article5["id"])  # user7 favorites article5
        # This should reorder the storages in output
        _, storage1 = storage_container.get_storage("session_1")
        # Call save_data to save all the populated data
        save_data()
        with self.TEST_DATA_FILE_PATH.open() as f:
            saved_data = json.loads(f.read())
        # Next line actually compares order
        self.assertEqual(json.dumps(saved_data), json.dumps(self.TEST_DATA_EXPECTED_FILE_CONTENT))

    @patch("realworld_dummy_server.log_structured")
    def test_load_data_complex(self, log_structured_mock):
        """Complex test for load_data using the same expected data structure"""
        global storage_container
        with self.TEST_DATA_FILE_PATH.open("w") as f:
            json.dump(self.TEST_DATA_EXPECTED_FILE_CONTENT, f)
        load_data()
        self.assertFalse(self.TEST_DATA_FILE_PATH.exists())  # ensure the existing file has been wiped on load
        save_data()  # we can trust save_data from previous test so we'll just reuse it
        with self.TEST_DATA_FILE_PATH.open() as f:
            loaded_data = json.loads(f.read())
        # Next line actually compares order
        self.assertEqual(json.dumps(loaded_data), json.dumps(self.TEST_DATA_EXPECTED_FILE_CONTENT))
