import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {performance} from 'node:perf_hooks';
import {runInNewContext} from 'node:vm';

// The browser uses native ESM; importing its source as a data URL lets this
// small standalone test run without changing the repository's package mode.
const source = readFileSync(new URL('../web/livewire-path.js', import.meta.url), 'utf8');
const {buildCostMap, buildShortestPaths, pathTo, MAX_LIVEWIRE_DIMENSION} =
  await import(`data:text/javascript;base64,${Buffer.from(source).toString('base64')}`);

function image(width, height, paint) {
  const pixels = new Uint8ClampedArray(width * height * 4);
  for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
    pixels.set(paint(x, y), (y * width + x) * 4);
  }
  return pixels;
}
function validPath(tree, x, y) {
  const points = pathTo(tree, x, y);
  assert.equal(points[0].x + points[0].y * tree.width, tree.seedIndex);
  assert.deepEqual(points.at(-1), {
    x: Math.max(0, Math.min(tree.width - 1, Math.round(x))),
    y: Math.max(0, Math.min(tree.height - 1, Math.round(y)))
  });
  assert(points.length <= tree.width * tree.height);
  for (let index = 1; index < points.length; index++) {
    const a = points[index - 1], b = points[index];
    assert(Math.abs(a.x - b.x) <= 1 && Math.abs(a.y - b.y) <= 1);
    assert(a.x !== b.x || a.y !== b.y);
    const parent = a.y * tree.width + a.x, child = b.y * tree.width + b.x;
    assert(tree.distances[child] > tree.distances[parent], 'the tree cannot contain a cycle');
  }
  return points;
}
function run(label, test) {
  const started = performance.now();
  test();
  console.log(`PASS ${label} (${Math.round(performance.now() - started)} ms)`);
}

run('rectangle boundary takes a corner rather than an interior chord', () => {
  const rgba = image(96, 96, (x, y) => x >= 18 && x <= 77 && y >= 18 && y <= 70 ? [20, 20, 20, 255] : [240, 240, 240, 255]);
  const tree = buildShortestPaths(buildCostMap(96, 96, rgba), 18, 34);
  const points = validPath(tree, 64, 70);
  const edgeDistance = ({x, y}) => Math.min(Math.abs(x - 18), Math.abs(x - 77), Math.abs(y - 18), Math.abs(y - 70));
  const meanError = points.reduce((sum, point) => sum + edgeDistance(point), 0) / points.length;
  assert(meanError < 1.2, `path should follow the rectangle edges (mean error ${meanError})`);
  assert(points.some(point => point.x <= 19 && point.y >= 68), 'path must reach the lower-left corner');
  assert(points.length > 60, 'a straight interior path is too short to trace this corner');
});

run('curved silhouette follows the arc, materially better than its chord', () => {
  const radius = 32, cx = 48, cy = 48;
  const rgba = image(96, 96, (x, y) => Math.hypot(x - cx, y - cy) <= radius ? [10, 30, 80, 255] : [245, 245, 245, 255]);
  const tree = buildShortestPaths(buildCostMap(96, 96, rgba), 16, 48);
  const points = validPath(tree, 48, 16);
  const error = ({x, y}) => Math.abs(Math.hypot(x - cx, y - cy) - radius);
  const pathError = points.reduce((sum, point) => sum + error(point), 0) / points.length;
  const chordError = Array.from({length: 101}, (_, index) => ({x: 16 + 32 * index / 100, y: 48 - 32 * index / 100}))
    .reduce((sum, point) => sum + error(point), 0) / 101;
  assert(pathError < 1.2, `path should follow the circle (mean error ${pathError})`);
  assert(pathError < chordError / 4, `edge path ${pathError} must improve on the chord ${chordError}`);
});

run('color boundaries remain visible at equal luminance', () => {
  const rgba = image(80, 80, (x, y) => x >= 15 && x <= 64 && y >= 15 && y <= 64 ? [255, 0, 0, 255] : [0, 130, 0, 255]);
  const tree = buildShortestPaths(buildCostMap(80, 80, rgba), 15, 30);
  const points = validPath(tree, 55, 64);
  assert(points.some(point => point.x <= 16 && point.y >= 62), 'RGB gradient must follow the color boundary corner');
});

run('flat image produces geometric distances and every heap node is reached', () => {
  const tree = buildShortestPaths(buildCostMap(31, 23, image(31, 23, () => [120, 120, 120, 255])), 9, 7);
  for (let y = 0; y < tree.height; y++) for (let x = 0; x < tree.width; x++) {
    const dx = Math.abs(x - 9), dy = Math.abs(y - 7);
    const expected = Math.min(dx, dy) * Math.SQRT2 + Math.abs(dx - dy);
    assert(Math.abs(tree.distances[y * tree.width + x] - expected) < 1e-9);
    validPath(tree, x, y);
  }
  assert.equal(pathTo(tree, 9, 7).length, 1);
});

run('degenerate grids, endpoints, malformed data, and cycle guard', () => {
  for (const [width, height] of [[1, 1], [1, 17], [19, 1]]) {
    const map = buildCostMap(width, height, image(width, height, (x, y) => [x * 11, y * 11, 30, 255]).buffer);
    const tree = buildShortestPaths(map, -500, -50);
    const points = validPath(tree, 500, 50);
    assert.equal(points.length, Math.max(width, height));
  }
  const offsetBuffer = new Uint8Array(12); offsetBuffer.set([30, 40, 50, 255], 4);
  assert.equal(buildCostMap(1, 1, offsetBuffer.subarray(4, 8)).strength[0], 0);
  assert.throws(() => buildCostMap(0, 1, new Uint8Array(0)));
  assert.throws(() => buildCostMap(MAX_LIVEWIRE_DIMENSION + 1, 1, new Uint8Array(0)));
  assert.throws(() => buildCostMap(2, 2, new Uint8Array(4)));
  const tree = buildShortestPaths(buildCostMap(2, 2, image(2, 2, () => [0, 0, 0, 255])), -1, 100);
  assert.equal(tree.seedIndex, 2);
  assert.throws(() => pathTo(tree, NaN, 1));
  tree.previous[0] = 1; tree.previous[1] = 0;
  assert.throws(() => pathTo(tree, 0, 0), /no valid path/);
});

run('bounded 512-pixel grid builds a full finite tree', () => {
  const size = MAX_LIVEWIRE_DIMENSION;
  const rgba = image(size, size, (x, y) => {
    const value = Math.hypot(x - 256, y - 256) < 190 ? 25 : 240;
    return [value, value, value, 255];
  });
  const tree = buildShortestPaths(buildCostMap(size, size, rgba), 66, 256);
  assert(tree.previous.every(index => index >= 0));
  assert(tree.distances.every(Number.isFinite));
  validPath(tree, 256, 66);
});

run('Worker protocol echoes IDs and invalidates old images or seeds on errors', () => {
  const replies = [], self = {postMessage: reply => replies.push(structuredClone(reply))};
  const workerSource = readFileSync(new URL('../web/livewire-worker.js', import.meta.url), 'utf8')
    .replace(/^import .*?;\n/, '');
  runInNewContext(workerSource, {self, buildCostMap, buildShortestPaths, pathTo});
  const request = data => {
    self.onmessage({data});
    const reply = replies.shift();
    assert.equal(reply.id, data.id);
    assert.equal(replies.length, 0, 'one response per request');
    return reply;
  };
  assert.match(request({type: 'path', id: 1, x: 0, y: 0}).error, /seed/);
  assert.deepEqual(request({type: 'init', id: 'image', width: 10, height: 8, pixels: image(10, 8, () => [50, 50, 50, 255]).buffer}), {id: 'image', type: 'ready'});
  assert.deepEqual(request({type: 'seed', id: 2, x: -100, y: 0}), {id: 2, type: 'ready'});
  const reply = request({type: 'path', id: 3, x: 100, y: 100});
  assert.equal(reply.type, 'path');
  assert.deepEqual(reply.points[0], {x: 0, y: 0});
  assert.deepEqual(reply.points.at(-1), {x: 9, y: 7});
  assert.match(request({type: 'seed', id: 4, x: NaN, y: 0}).error, /endpoint/);
  assert.match(request({type: 'path', id: 5, x: 0, y: 0}).error, /seed/);
  assert.deepEqual(request({type: 'seed', id: 6, x: 2, y: 1}), {id: 6, type: 'ready'});
  assert.match(request({type: 'init', id: 7, width: 0, height: 1, pixels: new ArrayBuffer(0)}).error, /dimensions/);
  assert.match(request({type: 'seed', id: 8, x: 0, y: 0}).error, /image/);
  assert.match(request({type: 'unknown', id: 9}).error, /Unknown/);
});

run('transparent and opaque white pixels have no artificial boundary', () => {
  const map = buildCostMap(24, 18, image(24, 18, x => x < 12 ? [0, 0, 0, 0] : [255, 255, 255, 255]));
  assert(map.strength.every(value => value === 0));
});
