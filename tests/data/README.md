# Third-party test data

## `tbls.schema.json`

The JSON Schema for the document format `stele dictionary` writes, taken
from [tbls](https://github.com/k1LoW/tbls) release `v1.96.0`:

<https://github.com/k1LoW/tbls/blob/v1.96.0/spec/tbls.schema.json_schema.json>

`tests/test_dictionary.py` validates every emitted document against it. The
copy is pinned rather than fetched so the tests need no network, and it is
the only independent statement of a contract stele does not control: tbls
reads a document without validating it, accepting both properties the schema
forbids and required properties that are absent, so rendering successfully is
not evidence of conformance. CI installs the matching tbls release, which
covers the other half — that a valid document renders.

Refreshing it means downloading the file at a newer tag, updating the version
here and the pin in `.github/workflows/ci.yml`, and running the tests.

### Licence

The MIT License (MIT)

Copyright © 2018 Ken'ichiro Oyama <k1lowxb@gmail.com>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
