"""Check how hyphenated JSX attribute names (aria-label, data-testid) and
boolean (no-value) attributes are tokenized, and how html attribute nodes
expose their name/value, before finalizing the ecmascript/twirl adapters.
"""

import tree_sitter_language_pack as p


def walk(node, depth=0):
    print("  " * depth, node.type, repr(node.text.decode()[:30]))
    for c in node.children:
        walk(c, depth + 1)


print("=== tsx: hyphenated + boolean JSX attrs ===")
parser = p.get_parser("tsx")
src = b'<Input aria-label="Name field" data-testid="x" disabled required />'
walk(parser.parse(src).root_node)

print()
print("=== html: attribute node field names ===")
parser = p.get_parser("html")
src = b'<input placeholder="Type here" aria-label="Search box" disabled/>'
tree = parser.parse(src)


def find(node, type_):
    if node.type == type_:
        yield node
    for c in node.children:
        yield from find(c, type_)


for attr in find(tree.root_node, "attribute"):
    for i, c in enumerate(attr.children):
        print("field:", attr.field_name_for_child(i), "->", c.type, repr(c.text.decode()))
