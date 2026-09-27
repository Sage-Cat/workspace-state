// Cached geometry; no Shell or clocks in this independently testable policy.
export function edgeTopology(monitors, primary) {
    if (!primary)
        return null;
    const rightEdge = primary.x + primary.width;
    let left = null;
    let right = null;
    for (const monitor of monitors) {
        if (monitor.x + monitor.width <= primary.x &&
            (!left || monitor.x + monitor.width > left.x + left.width))
            left = {...monitor};
        if (monitor.x >= rightEdge && (!right || monitor.x < right.x))
            right = {...monitor};
    }
    return {primary: {...primary}, left, right, rightEdge};
}

export function warpTarget(topology, x, y, elapsedMs) {
    if (!topology || elapsedMs < 250)
        return null;
    const {primary, left, right, rightEdge} = topology;
    if (left && left.y > primary.y && x === primary.x && y < left.y)
        return [left.x + left.width - 2, left.y + 1];
    if (right && right.y > primary.y && x === rightEdge - 1 && y < right.y)
        return [right.x + 1, right.y + 1];
    return null;
}
