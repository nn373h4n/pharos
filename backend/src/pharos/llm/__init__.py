"""The model layer (M4): everything that talks to a language model goes through `LLMGateway`.

Nothing is re-exported here on purpose: an import of the package would otherwise reach the gateway
through the package and slip past the import contracts that keep the API away from it.
"""
