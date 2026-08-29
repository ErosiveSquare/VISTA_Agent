"use strict";

const test = require("node:test");
const assert = require("node:assert");
const { paginate } = require("../src/paginate");

const items = Array.from({ length: 45 }, (_, i) => i + 1);

test("first page", () => {
  const r = paginate(items, 1, 20);
  assert.strictEqual(r.items.length, 20);
  assert.strictEqual(r.totalPages, 3);
});

test("last page", () => {
  const r = paginate(items, 3, 20);
  assert.strictEqual(r.items.length, 5);
});
