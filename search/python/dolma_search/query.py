import argparse
import json
import sys
from enum import Enum
from typing import Any, Generator, NamedTuple, Type

import jq
from markdownify import markdownify as md
from rich.console import Console
from rich.markdown import Markdown
from rich.table import Table
from rich.text import Text
from tantivy import Document, Query, Schema, Searcher, SnippetGenerator

from .common import IndexFields, create_index

import multiprocessing as mp
import os
from collections import defaultdict

from pathlib import Path

QUERY_DESCRIPTION = "Interactive search tool on a tantivy index"


class DisplayFormat(Enum):
    TABLE = "table"
    JSON = "json"
    SNIPPET = "snippet"


def make_search_parser(parser: argparse.ArgumentParser | None = None):
    parser = parser or argparse.ArgumentParser(QUERY_DESCRIPTION)
    parser.add_argument("-i", "--index-path", type=str, required=True, help="The path to the index.")
    parser.add_argument("-q", "--query", type=str, default=None, help="The query to search for.")
    parser.add_argument("-n", "--num-hits", type=int, default=10, help="The number of hits to return.")
    parser.add_argument("-d", "--filedir", type=str, default=None, help="Files to iterate over.")
    parser.add_argument("-o", "--outdir", type=str, default=None, help="Output directory.")
    parser.add_argument("-p", "--processes", type=int, default=1, help="Number of processes.")
    parser.add_argument(
        "-f",
        "--display-format",
        type=DisplayFormat,
        default=DisplayFormat.JSON,
        choices=list(DisplayFormat),
        help="The format to display the search results in.",
    )
    parser.add_argument(
        "-s",
        "--selector",
        type=str,
        default=None,
        help="The selector used to process the queries. Uses jq syntax.",
    )
    return parser


def query_iterator(query: str | None) -> Generator[str, None, None]:
    if query is None:
        while True:
            try:
                query = input("Enter a query: ")
                yield query
            except KeyboardInterrupt:
                print("\nExiting...")
                break
    elif query == "-":
        for line in sys.stdin:
            yield line.strip()
    else:
        yield str(query)


def apply_selector(queries: Generator[str, None, None], selector: str | None):
    selector = jq.compile(selector) if selector else None
    fn = lambda query: (str(e) for e in selector.input(json.loads(query)).all()) if selector else (str(query),)
    for query in queries:
        yield from fn(query)

def query_file_iterator(filepath):
    i = 0
    with open(filepath) as f:
        for line in f:
            i += 1
            yield(line.strip(),i)

def apply_selector_files(queries: Generator[str, None, None], selector: str | None):
    selector = jq.compile(selector) if selector else None
    fn = lambda query , i : ((str(e),i) for e in selector.input(json.loads(query)).all()) if selector else [(str(query),i)]
    for query,i in queries:
        yield from fn(query,i)


class HitsTuple(NamedTuple):
    score: float
    doc: dict[str, list[Any]]
    rank: int

    def get(self, field: str) -> str:
        return str(self.doc[field][0])

    def to_dict(self) -> dict[str, Any]:
        return {
            "document": {f.value: self.get(f.value) for f in IndexFields},
            "score": self.score,
            "rank": self.rank,
        }

    @classmethod
    def from_hits(cls: Type["HitsTuple"], hits: list[tuple[float, int]], searcher: Searcher) -> list["HitsTuple"]:
        return [
            cls(score=hit_score, doc=searcher.doc(hit_doc_address), rank=rank)  # pyright: ignore
            for rank, (hit_score, hit_doc_address) in enumerate(hits, start=1)
        ]


def print_hits_table(
    hits: list[HitsTuple],
    searcher: Searcher,
    schema: Schema,
    query: Query,
    show_snippets: bool = False,
    console: Console | None = None,
):
    console = console or Console()

    table = Table(title="Search Results", show_header=True, header_style="bold", show_lines=True)
    table.add_column("Score", justify="right", style="green")
    table.add_column(IndexFields.ID.value.upper(), style="magenta")
    table.add_column(IndexFields.SOURCE.value.capitalize(), style="cyan")
    table.add_column(IndexFields.TEXT.value.capitalize(), style="blue")

    for hit in hits:
        if show_snippets:
            snippet_generator = SnippetGenerator.create(
                searcher=searcher, query=query, schema=schema, field_name=IndexFields.TEXT.value
            )
            snippet = snippet_generator.snippet_from_doc(hit.doc)  # pyright: ignore
            hit_text = Markdown(md(snippet.to_html()).strip())
        else:
            hit_text = Text(hit.get(IndexFields.TEXT.value).strip().replace("\n", "\\n"))

        table.add_row(f"{hit.score:.2f}", hit.get("id"), hit.get("source"), str(hit_text))

    console.print(table)


def search_data(args: argparse.Namespace):
    index = create_index(args.index_path, reuse=True)
    searcher = index.searcher()

    console = Console()

    
    for query in apply_selector(query_iterator(args.query), args.selector):
        try:
            parsed_query = index.parse_query(query)
        except ValueError as e:
            raise ValueError(f"Error parsing query `{query}`: {e}")

        hits = searcher.search(parsed_query, limit=args.num_hits).hits
        parsed_hits = HitsTuple.from_hits(hits, searcher)  # pyright: ignore

        if args.display_format == DisplayFormat.JSON:
            for row in parsed_hits:
                print(json.dumps(row.to_dict(), sort_keys=True))
        else:
            print_hits_table(
                hits=parsed_hits,
                searcher=searcher,
                schema=index.schema,
                query=parsed_query,
                show_snippets=(args.display_format == DisplayFormat.SNIPPET),
                console=console,
            )


def search_data_files(args: argparse.Namespace):

    # index = create_index(args.index_path, reuse=True)
    # searcher = index.searcher()

    # console = Console()

    Path(args.outdir).mkdir(parents=True, exist_ok=True)
    
    with mp.Pool(processes=args.processes) as pool:
        results = defaultdict(list)
        tracker = {}
        for filename in os.listdir(args.filedir):
            filepath = os.path.join(args.filedir,filename)    
            tracker[filename] = 0
            for query,i in apply_selector_files(query_file_iterator(filepath), args.selector):
                result = pool.apply_async(search_one_query, (query,i,filename,args))
                results[filename].append(result)
            # print(f"FINISHED {filename}")
            # search_data_single_file(os.path.join(args.filedir,filename),args)
        
        pool.close()
        pool.join()

        for filename in results:
            basename = filename.split(".")[0]
            with open(f"{args.outdir}/{basename}.jsonl","w") as out:
                for result in results[filename]:
                    resultlist = result.get()
                    for jsonstring in resultlist:
                        out.write(jsonstring + "\n")



def search_one_query(query,i,filename,args):
    index = create_index(args.index_path, reuse=True)
    searcher = index.searcher()
    console = Console()

    # tracker[filename] += 1
    print(f"{filename}: query {i}")

    output = []
    try:
        parsed_query = index.parse_query(query)
    except ValueError as e:
        raise ValueError(f"Error parsing query `{query}`: {e}")

    hits = searcher.search(parsed_query, limit=args.num_hits).hits
    parsed_hits = HitsTuple.from_hits(hits, searcher)  # pyright: ignore

    if args.display_format == DisplayFormat.JSON:
        for row in parsed_hits:
            output.append(json.dumps(row.to_dict(), sort_keys=True))
            # output = "test_str"
    else:
        print_hits_table(
            hits=parsed_hits,
            searcher=searcher,
            schema=index.schema,
            query=parsed_query,
            show_snippets=(args.display_format == DisplayFormat.SNIPPET),
            console=console,
        )
    return output

# def search_data_single_file(filepath,args):
#     index = create_index(args.index_path, reuse=True)
#     searcher = index.searcher()

#     console = Console()

#     basename = filepath.split("/")[-1].split(".")[0]
#     print(basename)

#     with open(filepath) as f, open(f"{args.outdir}/{basename}.jsonl","w") as out:    
#         for query in apply_selector(query_file_iterator(f), args.selector):
#             try:
#                 parsed_query = index.parse_query(query)
#             except ValueError as e:
#                 raise ValueError(f"Error parsing query `{query}`: {e}")

#             hits = searcher.search(parsed_query, limit=args.num_hits).hits
#             parsed_hits = HitsTuple.from_hits(hits, searcher)  # pyright: ignore

#             if args.display_format == DisplayFormat.JSON:
#                 for row in parsed_hits:
#                     out.write(json.dumps(row.to_dict(), sort_keys=True) + "\n")
#             else:
#                 print_hits_table(
#                     hits=parsed_hits,
#                     searcher=searcher,
#                     schema=index.schema,
#                     query=parsed_query,
#                     show_snippets=(args.display_format == DisplayFormat.SNIPPET),
#                     console=console,
#                 )

def search_data_flex(args):
    if args.filedir is not None:
        search_data_files(args)
    else:
        search_data(args)

if __name__ == "__main__":
    search_data(make_search_parser().parse_args())
