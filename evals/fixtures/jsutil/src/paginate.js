"use strict";

/** 对数组做分页。 */
function paginate(items, page, perPage) {
  const p = Number(page) || 1;
  const size = Number(perPage) || 20;
  const start = (p - 1) * size;
  return {
    items: items.slice(start, start + size),
    page: p,
    perPage: size,
    total: items.length,
    totalPages: Math.ceil(items.length / size),
  };
}

module.exports = { paginate };
