"""CLI integration for bounded graph queries, context exports and local UI."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .index import IndexStore


def register_query_commands(subparsers) -> None:
    descriptions = {
        'contexts': 'List indexed build contexts.',
        'search': 'Search symbol names in the published index.',
        'symbol': 'Inspect a symbol and its indexed source locations.',
        'snippet': 'Read original source after checking its indexed hash.',
        'neighbors': 'Query callers, callees or other adjacent relationships.',
        'graph': 'Retrieve a bounded dependency subgraph.',
        'path': 'Find a bounded static dependency path.',
        'export': 'Export a source-based Markdown or JSON context pack.',
    }
    for name, description in descriptions.items():
        parser = subparsers.add_parser(name, help=description)
        parser.add_argument('--db', default='.legacygraph/index.db')
        parser.set_defaults(query_handler=run_query)
        if name == 'serve':
            parser.add_argument('--host', default='127.0.0.1')
            parser.add_argument('--port', type=int, default=8765)
            continue
        parser.add_argument('--json', action='store_true', help='Emit structured JSON (default for queries).')
        if name == 'contexts':
            continue
        parser.add_argument('--context', default=None, help='Exact context key; optional when only one is indexed.')
        if name == 'search':
            parser.add_argument('query', nargs='?', default='')
            parser.add_argument('--limit', type=int, default=50)
            parser.add_argument('--offset', type=int, default=0)
            continue
        if name == 'path':
            parser.add_argument('source')
            parser.add_argument('target')
        elif name == 'export':
            parser.add_argument('symbols', nargs='+', help='Semantic keys or unambiguous qualified names.')
            parser.add_argument('--target', default=None, help='Export the path to this symbol.')
            parser.add_argument('--format', choices=('markdown', 'json'), default='markdown')
            parser.add_argument('--output', type=Path, default=None)
            parser.add_argument('--token-budget', type=int, default=8000, help='Estimated token budget, not a model tokenizer cap.')
        else:
            parser.add_argument('symbol')
        if name == 'snippet':
            parser.add_argument('--max-lines', type=int, default=200)
        if name in ('neighbors', 'graph', 'path', 'export'):
            parser.add_argument('--direction', choices=('out', 'in', 'both'), default='out')
            parser.add_argument('--kinds', nargs='+', default=None)
        if name in ('neighbors', 'graph', 'path'):
            parser.add_argument('--statuses', nargs='+', default=None)
        if name == 'neighbors':
            parser.add_argument('--limit', type=int, default=200)
        if name in ('graph', 'export'):
            parser.add_argument('--depth', type=int, default=1)
            parser.add_argument('--max-nodes', type=int, default=100)
            parser.add_argument('--max-edges', type=int, default=300)
        if name == 'path':
            parser.add_argument('--max-depth', type=int, default=6)
            parser.add_argument('--max-nodes', type=int, default=10000)
            parser.add_argument('--max-edges', type=int, default=50000)
            parser.add_argument('--timeout', type=float, default=2.0)


def _context(service, requested):
    if requested:
        return requested
    contexts = service.contexts()
    active = [context for context in contexts if context.get('active_translation_units', 1) > 0]
    if active:
        contexts = active
    if len(contexts) != 1:
        raise ValueError('Choose --context explicitly; run `legacygraph contexts` to list available keys.')
    return contexts[0]['context_key']


def _symbol_key(service, value, context):
    from .queries import SymbolNotFoundError
    try:
        service.symbol(value, context)
        return value
    except SymbolNotFoundError:
        pass
    matches = service.search(value, context_key=context, limit=500)['items']
    exact = [item for item in matches if value in (item['name'], item['qualified_name'])]
    if len(exact) != 1:
        raise ValueError(f'Symbol {value!r} is missing or ambiguous; use its semantic_key from `search`.')
    return exact[0]['semantic_key']


def run_query(args: argparse.Namespace) -> int:
    try:
        if not Path(args.db).is_file():
            raise ValueError('Index database does not exist; run `scan` first.')
        if args.command == 'serve':
            from .web import serve
            serve(args.db, host=args.host, port=args.port)
            return 0
        from .queries import QueryService
        with IndexStore(args.db) as store:
            service = QueryService(store)
            if args.command == 'contexts':
                result = {'contexts': service.contexts()}
            elif args.command == 'search':
                result = service.search(args.query, context_key=args.context, limit=args.limit, offset=args.offset)
            else:
                context = _context(service, args.context)
                if args.command == 'path':
                    result = service.find_path(
                        _symbol_key(service, args.source, context), _symbol_key(service, args.target, context),
                        context, direction=args.direction, kinds=args.kinds, statuses=args.statuses,
                        max_depth=args.max_depth, max_nodes=args.max_nodes, max_edges=args.max_edges,
                        timeout_seconds=args.timeout,
                    )
                elif args.command == 'export':
                    from .context import ContextExporter
                    result = ContextExporter(service).export(
                        [_symbol_key(service, value, context) for value in args.symbols], context,
                        format=args.format, depth=args.depth, direction=args.direction, kinds=args.kinds,
                        token_budget=args.token_budget, max_nodes=args.max_nodes, max_edges=args.max_edges,
                        target=_symbol_key(service, args.target, context) if args.target else None,
                    )
                    if result.get('budget_insufficient'):
                        if args.json:
                            print(json.dumps(result, ensure_ascii=False, indent=2))
                        else:
                            print('Error: budget insufficient for mandatory seed/path content.', file=sys.stderr)
                        return 3
                    if args.output:
                        output = args.output.resolve()
                        if output.exists():
                            raise ValueError('Output already exists; choose a new context-pack path.')
                        if output.suffix.lower() not in ('.md', '.json'):
                            raise ValueError('Context output must use .md or .json extension.')
                        if output == Path(args.db).resolve():
                            raise ValueError('Context output cannot replace the index.')
                        # Explicit exports may be inside the repository; exclusive creation
                        # protects existing source files and other user data.
                        output.parent.mkdir(parents=True, exist_ok=True)
                        with output.open('x', encoding='utf-8', newline='\n') as stream:
                            stream.write(result['content'])
                        print(json.dumps({'output': str(output), 'metadata': result.get('metadata', {})}, ensure_ascii=False))
                    elif getattr(args, 'json', False):
                        print(json.dumps(result, ensure_ascii=False, indent=2))
                    else:
                        print(result['content'])
                    return 0
                else:
                    key = _symbol_key(service, args.symbol, context)
                    if args.command == 'symbol':
                        result = service.symbol(key, context)
                    elif args.command == 'snippet':
                        result = service.snippet(key, context, max_lines=args.max_lines)
                    elif args.command == 'neighbors':
                        result = service.neighbors(key, context, direction=args.direction, kinds=args.kinds,
                                                   statuses=args.statuses, limit=args.limit)
                    else:
                        result = service.subgraph([key], context, direction=args.direction, depth=args.depth,
                                                  kinds=args.kinds, statuses=args.statuses,
                                                  max_nodes=args.max_nodes, max_edges=args.max_edges)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, KeyError, OSError, sqlite3.Error, ImportError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        return 1
