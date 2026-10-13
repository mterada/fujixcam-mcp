# MCP Camera Server Project

## Project Overview
Python-based MCP (Model Context Protocol) server for FUJIFILM X/GFX Digital Camera control.

## Features
- Manage camera connection lifecycle (connect, disconnect)
- Query camera status and settings
- Query camera options (exposure settings, focus settings, ...)
- Apply camera setting changes (exposure settings, focus settings, ...)
- Capture images to local storage and return the file path to the agent
- Catpure live view images from the camera and return them to the agent

## Technology Stack
- Python 3.11+
- MCP Python SDK
- fujixcam Python package for camera control
- FUJIFILM Digital Camera control SDK (official SDK provided by FUJIFILM)
- Pillow for image processing

## Project Status
- [x] Create copilot-instructions.md file
- [x] Get project setup info for MCP server
- [x] Scaffold Python MCP server project structure
- [x] Create camera interface module
- [x] Create MCP server implementation
- [x] Create configuration files
- [x] Create documentation
- [x] Finalize project setup
