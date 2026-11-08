# Reference MIME fixtures

These are synthetic codec fixtures, not customer media or downloaded results.
`reference-header.webp` is a valid 13,036-byte WebP generated locally from a
deterministic 128 × 128 RGB sample (Python `random.Random(20261108).randbytes`,
Pillow WebP quality 86). The JPEG and PNG use a 2 × 2 version of the same sample.
Pillow is only used to create these committed fixtures; the regression suite
reads the bytes and does not add a Pillow dependency to the application or CI.

The WebP reproduces the relevant production shape: an approximately 13 KB raw
WebP body served at a `.jpg` URL with `Content-Type: multipart/form-data`.
The test verifies MIME correction without modifying the original bytes.
