// Image-space intelligent scissors. All image analysis and shortest-path work
// runs in livewire-worker.js; the pure functions also make the graph testable.
export const MAX_LIVEWIRE_DIMENSION = 512;

const SQRT2 = Math.SQRT2;
const DIRECTIONS = [
  [-1, -1, SQRT2], [0, -1, 1], [1, -1, SQRT2],
  [-1, 0, 1], [1, 0, 1],
  [-1, 1, SQRT2], [0, 1, 1], [1, 1, SQRT2]
];

function dimensions(width, height) {
  if (!Number.isInteger(width) || !Number.isInteger(height) || width < 1 || height < 1 ||
      width > MAX_LIVEWIRE_DIMENSION || height > MAX_LIVEWIRE_DIMENSION) {
    throw new Error(`Livewire image dimensions must be 1–${MAX_LIVEWIRE_DIMENSION} pixels.`);
  }
  return width * height;
}

function endpoint(width, height, x, y) {
  if (!Number.isFinite(x) || !Number.isFinite(y)) throw new Error('Livewire endpoint is invalid.');
  return Math.max(0, Math.min(height - 1, Math.round(y))) * width +
    Math.max(0, Math.min(width - 1, Math.round(x)));
}

export function buildCostMap(width, height, rgba) {
  const count = dimensions(width, height);
  let bytes;
  if (rgba instanceof ArrayBuffer) bytes = new Uint8Array(rgba);
  else if (ArrayBuffer.isView(rgba)) bytes = new Uint8Array(rgba.buffer, rgba.byteOffset, rgba.byteLength);
  if (!bytes || bytes.length !== count * 4) throw new Error('Livewire RGBA pixels do not match the image dimensions.');

  // Analyze RGB rather than luminance alone: equally bright color boundaries
  // still need a path. Transparent pixels are composited over the white stage.
  const red = new Float32Array(count), green = new Float32Array(count), blue = new Float32Array(count);
  for (let index = 0; index < count; index++) {
    const offset = index * 4, alpha = bytes[offset + 3] / 255, white = 1 - alpha;
    red[index] = bytes[offset] / 255 * alpha + white;
    green[index] = bytes[offset + 1] / 255 * alpha + white;
    blue[index] = bytes[offset + 2] / 255 * alpha + white;
  }

  const strength = new Float32Array(count), gradientX = new Float32Array(count), gradientY = new Float32Array(count);
  let maximum = 0;
  for (let y = 0; y < height; y++) {
    const above = Math.max(0, y - 1) * width, row = y * width, below = Math.min(height - 1, y + 1) * width;
    for (let x = 0; x < width; x++) {
      const left = Math.max(0, x - 1), right = Math.min(width - 1, x + 1);
      const a = above + left, b = above + x, c = above + right;
      const d = row + left, f = row + right;
      const g = below + left, h = below + x, i = below + right;
      let xx = 0, yy = 0, xy = 0;
      for (const channel of [red, green, blue]) {
        const gx = -channel[a] + channel[c] - 2 * channel[d] + 2 * channel[f] - channel[g] + channel[i];
        const gy = -channel[a] - 2 * channel[b] - channel[c] + channel[g] + 2 * channel[h] + channel[i];
        xx += gx * gx; yy += gy * gy; xy += gx * gy;
      }
      const index = row + x, magnitude = Math.sqrt(xx + yy);
      strength[index] = magnitude;
      maximum = Math.max(maximum, magnitude);
      if (magnitude > 0) {
        // The leading eigenvector of the RGB gradient structure tensor gives
        // the edge normal. Its sign is immaterial to the tangent cost below.
        const angle = 0.5 * Math.atan2(2 * xy, xx - yy);
        gradientX[index] = Math.cos(angle); gradientY[index] = Math.sin(angle);
      }
    }
  }
  if (maximum > 0) {
    for (let index = 0; index < count; index++) strength[index] /= maximum;
  }
  return {width, height, strength, gradientX, gradientY};
}

// One heap entry per pixel (decrease-key rather than duplicate entries) bounds
// memory even when many candidate paths improve the same pixel repeatedly.
class PixelHeap {
  constructor(distances) {
    this.distances = distances;
    this.nodes = new Int32Array(distances.length);
    this.positions = new Int32Array(distances.length).fill(-1);
    this.length = 0;
  }
  less(a, b) {
    return this.distances[a] < this.distances[b] ||
      (this.distances[a] === this.distances[b] && a < b);
  }
  decrease(node) {
    let position = this.positions[node];
    if (position < 0) position = this.length++;
    while (position > 0) {
      const parent = (position - 1) >> 1, other = this.nodes[parent];
      if (!this.less(node, other)) break;
      this.nodes[position] = other; this.positions[other] = position;
      position = parent;
    }
    this.nodes[position] = node; this.positions[node] = position;
  }
  pop() {
    const result = this.nodes[0], tail = this.nodes[--this.length];
    this.positions[result] = -1;
    if (this.length) {
      let position = 0;
      while (true) {
        const left = 2 * position + 1;
        if (left >= this.length) break;
        const right = left + 1;
        const child = right < this.length && this.less(this.nodes[right], this.nodes[left]) ? right : left;
        const other = this.nodes[child];
        if (!this.less(other, tail)) break;
        this.nodes[position] = other; this.positions[other] = position;
        position = child;
      }
      this.nodes[position] = tail; this.positions[tail] = position;
    }
    return result;
  }
}

export function buildShortestPaths(costMap, x, y) {
  const {width, height, strength, gradientX, gradientY} = costMap || {};
  const count = dimensions(width, height);
  if (![strength, gradientX, gradientY].every(array => array instanceof Float32Array && array.length === count)) {
    throw new Error('Livewire cost map is invalid.');
  }
  const seedIndex = endpoint(width, height, x, y);
  const distances = new Float64Array(count).fill(Infinity);
  const previous = new Int32Array(count).fill(-1), settled = new Uint8Array(count);
  const heap = new PixelHeap(distances);
  distances[seedIndex] = 0; previous[seedIndex] = seedIndex; heap.decrease(seedIndex);
  while (heap.length) {
    const node = heap.pop(), row = Math.floor(node / width), column = node % width;
    settled[node] = 1;
    for (const [dx, dy, length] of DIRECTIONS) {
      const nextX = column + dx, nextY = row + dy;
      if (nextX < 0 || nextX >= width || nextY < 0 || nextY >= height) continue;
      const next = nextY * width + nextX;
      if (settled[next]) continue;
      const edge = (strength[node] + strength[next]) * 0.5;
      const across = (strength[node] * Math.abs(dx * gradientX[node] + dy * gradientY[node]) +
        strength[next] * Math.abs(dx * gradientX[next] + dy * gradientY[next])) * 0.5 / length;
      // Positive weights make the tree acyclic. The physical step length keeps
      // flat areas geometric; the normal penalty rewards travel along edges.
      const weight = length * (0.03 + 0.97 * (1 - edge) ** 2 + 0.4 * across);
      const candidate = distances[node] + weight;
      if (candidate < distances[next]) {
        distances[next] = candidate; previous[next] = node; heap.decrease(next);
      }
    }
  }
  return {width, height, seedIndex, previous, distances};
}

export function pathTo(tree, x, y) {
  const {width, height, seedIndex, previous} = tree || {};
  const count = dimensions(width, height);
  if (!(previous instanceof Int32Array) || previous.length !== count ||
      !Number.isInteger(seedIndex) || seedIndex < 0 || seedIndex >= count) throw new Error('Livewire path tree is invalid.');
  const points = [];
  let node = endpoint(width, height, x, y);
  for (let steps = 0; steps < count; steps++) {
    points.push({x: node % width, y: Math.floor(node / width)});
    if (node === seedIndex) return points.reverse();
    node = previous[node];
    if (node < 0 || node >= count) break;
  }
  throw new Error('Livewire endpoint has no valid path.');
}
