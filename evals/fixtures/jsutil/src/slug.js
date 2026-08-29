"use strict";

/** 把标题转成 URL slug。 */
function slugify(input) {
  if (typeof input !== "string") return "";
  return input
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

module.exports = { slugify };
