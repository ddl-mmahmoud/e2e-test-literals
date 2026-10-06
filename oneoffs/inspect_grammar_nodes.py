"""Exploratory: inspect tree-sitter node shapes for template strings,
javascript-grammar JSX, and the html grammar, to nail down field names
before writing the language adapters.
"""

import tree_sitter_language_pack as p


def walk(node, depth=0):
    print("  " * depth, node.type)
    for c in node.children:
        walk(c, depth + 1)


print("=== tsx: template strings ===")
parser = p.get_parser("tsx")
src = b"const s = `plain template`;\nconst s2 = `with ${x} sub`;\n"
walk(parser.parse(src).root_node)

print()
print("=== javascript: JSX + object property ===")
parser = p.get_parser("javascript")
src = b'const o = { title: "Hi" }; function F(){ return <div className="x">Text here</div>; }'
walk(parser.parse(src).root_node)

print()
print("=== html: text + attributes ===")
parser = p.get_parser("html")
src = b'<div><p>Some paragraph text</p><input placeholder="Type here" class="x"/></div>'
walk(parser.parse(src).root_node)
