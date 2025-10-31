from pathlib import Path
from typing import Optional, List, Dict, Union, Set
from dataclasses import dataclass
import mcp.types as types
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
import mcp.server.stdio
import os
from pathspec import PathSpec
from pathspec.patterns import GitWildMatchPattern

@dataclass
class Summary:
    file_count: int = 0
    dir_count: int = 0
    total_size: int = 0

@dataclass
class FileStructure:
    path: str
    type: str
    size: Optional[int] = None
    children: Optional[List['FileStructure']] = None
    summary: Optional[Summary] = None

class RepoStructureAnalyzer:
    def __init__(self, repo_path: Path, max_depth: int = 3, max_children: int = 100):
        self.repo_path = repo_path
        self.MAX_DEPTH = max_depth
        self.MAX_CHILDREN = max_children
        
        # Default patterns to always ignore
        self.default_ignore_patterns = ['.git', '__pycache__', 'node_modules']
        
        # Load .gitignore if it exists
        self.gitignore_spec = self._load_gitignore()

    def _load_gitignore(self) -> Optional[PathSpec]:
        """Load .gitignore patterns if the file exists."""
        gitignore_path = self.repo_path / '.gitignore'
        patterns = []
        
        # Add default patterns
        for pattern in self.default_ignore_patterns:
            patterns.append(GitWildMatchPattern(pattern))

        # Add patterns from .gitignore if it exists
        if gitignore_path.exists() and gitignore_path.is_file():
            try:
                with open(gitignore_path, 'r') as f:
                    for line in f:
                        line = line.strip()
                        # Skip empty lines and comments
                        if line and not line.startswith('#'):
                            patterns.append(GitWildMatchPattern(line))
            except Exception as e:
                print(f"Error reading .gitignore: {e}")
        
        return PathSpec(patterns)

    def _is_safe_path(self, path: Path) -> bool:
        """Check if the given path is safe (no directory traversal)."""
        try:
            return path.resolve().is_relative_to(self.repo_path.resolve())
        except ValueError:
            return False

    def format_structure(self, structure: FileStructure) -> str:
        """Format the file structure into a readable string."""
        output = []

        def format_size(size: int) -> str:
            units = ['B', 'KB', 'MB', 'GB']
            value = float(size)
            index = 0
            while value >= 1024 and index < len(units) - 1:
                value /= 1024
                index += 1
            return f"{value:.1f} {units[index]}"

        def format_item(item: FileStructure, level: int = 0) -> None:
            indent = '  ' * level
            
            if item.type == 'directory':
                output.append(f"{indent}📁 {item.path}/")
                
                if item.summary:
                    summary = item.summary
                    output.append(
                        f"{indent}   Contains: {summary.file_count} files, "
                        f"{summary.dir_count} directories, "
                        f"{format_size(summary.total_size)}"
                    )
                elif item.children:
                    for child in item.children:
                        format_item(child, level + 1)
            else:
                output.append(f"{indent}📄 {item.path} ({format_size(item.size or 0)})")

        format_item(structure)
        return '\n'.join(output)

    def should_ignore(self, path: str) -> bool:
        """Check if path should be ignored based on gitignore patterns."""
        return self.gitignore_spec.match_file(path)

    def get_structure(
        self,
        current_path: Path,
        relative_path: str = '',
        current_depth: int = 0,
        max_depth: Optional[int] = None
    ) -> FileStructure:
        """Recursively get the structure of files and directories."""
        if max_depth is None:
            max_depth = self.MAX_DEPTH

        # Verify path safety
        if not self._is_safe_path(current_path):
            raise ValueError(f"Invalid path: {current_path}")

        # Skip symbolic links
        if current_path.is_symlink():
            raise ValueError(f"Symbolic links are not supported: {current_path}")

        stats = current_path.stat()
        rel_path = relative_path or current_path.name

        if relative_path and self.should_ignore(relative_path):
            raise ValueError(f"Path {relative_path} is ignored")

        if current_path.is_file():
            return FileStructure(
                path=rel_path,
                type="file",
                size=stats.st_size
            )

        if current_path.is_dir():
            children = []
            summary = Summary()

            if current_depth < max_depth:
                try:
                    entries = list(current_path.iterdir())
                    
                    for entry in entries:
                        if len(children) >= self.MAX_CHILDREN:
                            if entry.is_file() and not entry.is_symlink():
                                summary.file_count += 1
                                summary.total_size += entry.stat().st_size
                            elif entry.is_dir() and not entry.is_symlink():
                                summary.dir_count += 1
                            continue

                        entry_relative_path = str(entry.relative_to(self.repo_path))

                        try:
                            if self.should_ignore(entry_relative_path):
                                continue

                            if entry.is_symlink():
                                continue

                            entry_stats = entry.stat()
                            
                            if entry.is_file():
                                summary.file_count += 1
                                summary.total_size += entry_stats.st_size
                            elif entry.is_dir():
                                summary.dir_count += 1

                            child = self.get_structure(
                                entry,
                                entry_relative_path,
                                current_depth + 1,
                                max_depth
                            )
                            children.append(child)

                        except Exception as error:
                            print(f"Error processing {entry}: {error}")
                            continue

                except Exception as error:
                    print(f"Error reading directory {current_path}: {error}")

            return FileStructure(
                path=rel_path,
                type="directory",
                children=children if children else None,
                summary=summary if current_depth >= max_depth or len(children) >= self.MAX_CHILDREN else None
            )

        raise ValueError(f"Unsupported file type at {current_path}")

class FileReader:
    def __init__(self, repo_path: Path):
        self.repo_path = repo_path
        self.MAX_SIZE = 1024 * 1024  # 1MB
        self.MAX_LINES = 1000  # Maximum number of lines to return

    def _detect_language(self, file_path: str) -> str:
        """Detect the programming language based on file extension."""
        ext = Path(file_path).suffix.lower()
        
        # Extensive mapping of file extensions to languages
        language_map = {
            # Programming Languages
            '.py': 'python',
            '.js': 'javascript',
            '.jsx': 'javascript',
            '.ts': 'typescript',
            '.tsx': 'typescript',
            '.java': 'java',
            '.cpp': 'cpp',
            '.cc': 'cpp',
            '.hpp': 'cpp',
            '.c': 'c',
            '.h': 'c',
            '.cs': 'csharp',
            '.rb': 'ruby',
            '.php': 'php',
            '.go': 'go',
            '.rs': 'rust',
            '.swift': 'swift',
            '.kt': 'kotlin',
            '.scala': 'scala',
            '.m': 'objective-c',
            '.mm': 'objective-c',
            
            # Web Technologies
            '.html': 'html',
            '.htm': 'html',
            '.css': 'css',
            '.scss': 'scss',
            '.sass': 'scss',
            '.less': 'less',
            '.vue': 'vue',
            '.svelte': 'svelte',
            
            # Data & Config Files
            '.json': 'json',
            '.xml': 'xml',
            '.yaml': 'yaml',
            '.yml': 'yaml',
            '.toml': 'toml',
            '.ini': 'ini',
            '.conf': 'config',
            
            # Documentation
            '.md': 'markdown',
            '.markdown': 'markdown',
            '.rst': 'restructuredtext',
            '.tex': 'latex',
            
            # Shell Scripts
            '.sh': 'shell',
            '.bash': 'shell',
            '.zsh': 'shell',
            '.fish': 'shell',
            '.bat': 'batch',
            '.cmd': 'batch',
            '.ps1': 'powershell',
            
            # Other Common Types
            '.sql': 'sql',
            '.r': 'r',
            '.gradle': 'gradle',
            '.dockerfile': 'dockerfile',
            '.env': 'env',
            '.gitignore': 'gitignore'
        }
        
        # Handle files without extension but specific names
        if not ext:
            filename = Path(file_path).name.lower()
            name_map = {
                'dockerfile': 'dockerfile',
                'makefile': 'makefile',
                'jenkinsfile': 'jenkinsfile',
                'vagrantfile': 'ruby',
                '.env': 'env',
                '.gitignore': 'gitignore'
            }
            return name_map.get(filename, 'text')
            
        return language_map.get(ext, 'text')

    def read_file(self, file_path: str) -> Dict[str, Union[List[Dict[str, str]], bool]]:
        """Read and format file contents for LLM consumption."""
        try:
            full_path = self.repo_path / file_path
            
            # Check if path is safe
            if not full_path.resolve().is_relative_to(self.repo_path.resolve()):
                return {
                    "content": [{
                        "type": "text",
                        "text": f"Error: Attempted to access file outside repository: {file_path}"
                    }],
                    "isError": True
                }
            
            # Check if file exists
            if not full_path.exists():
                return {
                    "content": [{
                        "type": "text",
                        "text": f"File {file_path} not found"
                    }],
                    "isError": True
                }
            
            # Check if it's a symbolic link
            if full_path.is_symlink():
                return {
                    "content": [{
                        "type": "text",
                        "text": f"Error: Symbolic links are not supported: {file_path}"
                    }],
                    "isError": True
                }
            
            # Get file stats
            stats = full_path.stat()
            
            # Check file size
            if stats.st_size > self.MAX_SIZE:
                return {
                    "content": [{
                        "type": "text",
                        "text": f"File {file_path} is too large ({stats.st_size} bytes). "
                             f"Maximum size is {self.MAX_SIZE} bytes."
                    }],
                    "isError": True
                }
            
            # Read file content with line limit
            lines = []
            line_count = 0
            truncated = False
            
            with open(full_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line_count += 1
                    if line_count <= self.MAX_LINES:
                        lines.append(line.rstrip('\n'))
                    else:
                        truncated = True
                        break
            
            content = '\n'.join(lines)
            if truncated:
                content += f"\n\n[File truncated after {self.MAX_LINES} lines]"
            
            # Detect language
            language = self._detect_language(file_path)
            
            return {
                "content": [{
                    "type": "text",
                    "text": f"File: {file_path}\n"
                           f"Language: {language}\n"
                           f"Size: {stats.st_size} bytes\n"
                           f"Total lines: {line_count}\n\n"
                           f"{content}"
                }]
            }
            
        except UnicodeDecodeError:
            return {
                "content": [{
                    "type": "text",
                    "text": f"Error: File {file_path} appears to be a binary file"
                }],
                "isError": True
            }
        except Exception as error:
            return {
                "content": [{
                    "type": "text",
                    "text": f"Error reading file: {str(error)}"
                }],
                "isError": True
            }

# Global state
repo_path: Optional[Path] = None
analyzer: Optional[RepoStructureAnalyzer] = None
file_reader: Optional[FileReader] = None

# Initialize server
server = Server("code-analysis")

def initialize_repo(path: str) -> None:
    """Initialize the repository path and analysis tools."""
    global repo_path, analyzer, file_reader
    
    if not path or path in (".", "./"):
        raise ValueError("Repository path must be an absolute path")
        
    repo_path_obj = Path(path).resolve()
    if not repo_path_obj.is_absolute():
        raise ValueError(f"Repository path must be absolute, got: {repo_path_obj}")
    if not repo_path_obj.exists():
        raise ValueError(f"Repository path does not exist: {repo_path_obj}")
    if not repo_path_obj.is_dir():
        raise ValueError(f"Repository path is not a directory: {repo_path_obj}")
    
    repo_path = repo_path_obj
    analyzer = RepoStructureAnalyzer(repo_path)
    file_reader = FileReader(repo_path)

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """List available tools."""
    return [
        types.Tool(
            name="initialize_repository",
            description="Initialize the repository path for future code analysis operations",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the repository root directory that contains the code to analyze"
                    }
                },
                "required": ["path"]
            }
        ),
        types.Tool(
            name="get_repo_info",
            description="Get information about the currently initialized code repository",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        types.Tool(
            name="get_repo_structure",
            description="Get the structure of files and directories in the repository",
            inputSchema={
                "type": "object",
                "properties": {
                    "sub_path": {
                        "type": "string",
                        "description": "Optional subdirectory path relative to repository root"
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Optional maximum depth to traverse (default is 3)"
                    }
                }
            }
        ),
        types.Tool(
            name="read_file",
            description="Read and display the contents of a file from the repository",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to the file relative to repository root"
                    }
                },
                "required": ["file_path"]
            }
        )
    ]

@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    """Handle tool calls."""
    global repo_path, analyzer, file_reader
    
    if name == "initialize_repository":
        try:
            path = arguments["path"]
            initialize_repo(path)
            gitignore_path = Path(path) / '.gitignore'
            gitignore_status = "Found .gitignore file" if gitignore_path.exists() else "No .gitignore file present"
            result = f"Successfully initialized code repository at: {repo_path}\n{gitignore_status}"
        except ValueError as e:
            result = f"Error initializing code repository: {str(e)}"
        except Exception as e:
            result = f"Unexpected error: {str(e)}"
        
        return [types.TextContent(type="text", text=result)]
    
    elif name == "get_repo_info":
        if not repo_path:
            result = "No code repository has been initialized yet. Please use initialize_repository first."
        else:
            gitignore_path = repo_path / '.gitignore'
            gitignore_status = "Found .gitignore file" if gitignore_path.exists() else "No .gitignore file present"
            
            result = f"""Code Repository Information:
Path: {repo_path}
Exists: {repo_path.exists()}
Is Directory: {repo_path.is_dir()}
{gitignore_status}"""
        
        return [types.TextContent(type="text", text=result)]
    
    elif name == "get_repo_structure":
        if not repo_path or not analyzer:
            result = "No code repository has been initialized yet. Please use initialize_repository first."
        else:
            try:
                sub_path = arguments.get("sub_path")
                depth = arguments.get("depth")
                
                target_path = repo_path
                if sub_path:
                    target_path = repo_path / sub_path
                    if not analyzer._is_safe_path(target_path):
                        result = "Error: Invalid path - directory traversal not allowed"
                        return [types.TextContent(type="text", text=result)]

                structure = analyzer.get_structure(
                    target_path,
                    sub_path or '',
                    max_depth=depth
                )
                result = analyzer.format_structure(structure)
            except Exception as e:
                result = f"Error analyzing repository structure: {str(e)}"
        
        return [types.TextContent(type="text", text=result)]
    
    elif name == "read_file":
        if not repo_path or not file_reader or not analyzer:
            result = "No code repository has been initialized yet. Please use initialize_repository first."
        else:
            try:
                file_path = arguments["file_path"]
                
                # Check if file should be ignored based on gitignore patterns
                if analyzer.should_ignore(file_path):
                    result = f"File {file_path} is ignored based on .gitignore patterns"
                else:
                    file_result = file_reader.read_file(file_path)
                    
                    if file_result.get("isError", False):
                        result = file_result["content"][0]["text"]
                    else:
                        result = file_result["content"][0]["text"]
                        
            except Exception as e:
                result = f"Error reading file: {str(e)}"
        
        return [types.TextContent(type="text", text=result)]
    
    else:
        raise ValueError(f"Unknown tool: {name}")

async def main():
    """Main entry point for the MCP server."""
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="code-analysis",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )

def cli_main():
    """Synchronous entry point for CLI."""
    import asyncio
    asyncio.run(main())

if __name__ == "__main__":
    cli_main()