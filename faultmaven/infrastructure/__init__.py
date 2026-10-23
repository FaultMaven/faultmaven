"""Shared adapters between FaultMaven's modules and the systems it runs on.

Databases, vector and object stores, caches, LLM providers and other external
services are reached through the code here. Import a name from the module that
defines it: this ``__init__`` imports nothing, so loading one adapter does not
load the rest (#2187).
"""
