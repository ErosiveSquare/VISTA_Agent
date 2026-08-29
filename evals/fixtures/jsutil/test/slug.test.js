"use strict";

const test = require("node:test");
const assert = require("node:assert");
const { slugify } = require("../src/slug");

test("basic slug", () => {
  assert.strictEqual(slugify("Hello World"), "hello-world");
});

test("strips punctuation", () => {
  assert.strictEqual(slugify("  A, B & C!  "), "a-b-c");
});

test("non-string input", () => {
  assert.strictEqual(slugify(null), "");
});
