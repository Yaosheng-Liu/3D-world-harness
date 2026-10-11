import {buildCostMap, buildShortestPaths, pathTo} from './livewire-path.js';

let costMap = null, tree = null;

self.onmessage = ({data}) => {
  const id = data?.id;
  try {
    switch (data?.type) {
      case 'init':
        costMap = null; tree = null;
        costMap = buildCostMap(data.width, data.height, data.pixels);
        self.postMessage({id, type: 'ready'});
        break;
      case 'seed':
        if (!costMap) throw new Error('Livewire image is not ready.');
        tree = null;
        tree = buildShortestPaths(costMap, data.x, data.y);
        self.postMessage({id, type: 'ready'});
        break;
      case 'path':
        if (!tree) throw new Error('Livewire seed is not ready.');
        self.postMessage({id, type: 'path', points: pathTo(tree, data.x, data.y)});
        break;
      default:
        throw new Error('Unknown Livewire request.');
    }
  } catch (error) {
    self.postMessage({id, error: error?.message || 'Livewire path generation failed.'});
  }
};
