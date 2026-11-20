//! The light sources a `[ray_tracing] lights = true` pack's rays aim at (docs/api/shaders.md, the
//! prelude's `ray_light`, `ray_lights_near` and `ray_light_at`): the sources nearest the camera and a
//! grid of cells over them, each listing the sources whose light reaches it, strongest first.
//! A pass shades a point with the few sources its cell lists, each tested with a shadow ray,
//! instead of every source in the world.
//!
//! The buffer is rebuilt on the CPU when the camera moves on to another 16-block step or the
//! light sources change, from the sources the section meshes list (`SectionMesh::lights`); its
//! corner, relative to the player's camera (`frame.camera_position`), is written every frame.

use crate::fast_hash::FxHashMap;
use glam::{DVec3, IVec3, Vec3};
use horizon_shader_api::stages::{
    RAY_LIGHT_CELL, RAY_LIGHT_GRID, RAY_LIGHT_HEADER, RAY_LIGHT_MAX, RAY_LIGHT_PER_CELL,
    RAY_LIGHT_WORDS,
};

use super::SectionKey;
use horizon_items::blocks::Fluid;

/// The grid moves in steps of this many blocks.
const STEP: i32 = 16;
/// Where the cells' counts start, and then their lists: after the header and the sources.
const COUNTS: u32 = RAY_LIGHT_HEADER + RAY_LIGHT_MAX * RAY_LIGHT_WORDS;
const CELLS: u32 = RAY_LIGHT_GRID * RAY_LIGHT_GRID * RAY_LIGHT_GRID;
const LISTS: u32 = COUNTS + CELLS;
/// The buffer's words.
const WORDS: u32 = LISTS + CELLS * RAY_LIGHT_PER_CELL;

/// What `ray_light` reads of a light-giving block state.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Source {
    /// Its `[[lights]]` colour as written (the prelude decodes it), white where it has none.
    pub color: [f32; 3],
    /// Vanilla's light level, 1 to 15.
    pub level: u8,
    /// Its `[[materials]]` id, 0 for none (`material_of`).
    pub material: u32,
}

/// Each listed block state's `Source`, by state id.
pub type Sources = FxHashMap<u32, Source>;

/// The source a block state lists, if it gives off light of its own: not a fluid's (lava; a
/// lantern under water is listed), whose lakes would fill every cell. `color` is its
/// `[[lights]]` colour, `layers` the sprite layers its model's faces draw with, in order, and
/// `materials` the pack's `(layer, id)`, by layer.
pub fn source(
    (emission, fluid): (u8, Fluid),
    color: Option<[f32; 3]>,
    layers: impl IntoIterator<Item = u32>,
    materials: &[(u32, u32)],
) -> Option<Source> {
    let fluid_light = matches!(fluid, Fluid::Lava | Fluid::Modded);
    (emission > 0 && !fluid_light).then(|| Source {
        color: color.unwrap_or([1.0; 3]),
        level: emission,
        material: material_of(layers, materials),
    })
}

/// The id `RayHit.material` reports on the first face, of `layers`, whose sprite layer has one
/// in `materials` (sorted by layer); 0 where none does.
fn material_of(layers: impl IntoIterator<Item = u32>, materials: &[(u32, u32)]) -> u32 {
    let id = |layer| (materials.binary_search_by_key(&layer, |m| m.0).ok()).map(|i| materials[i].1);
    layers.into_iter().find_map(id).unwrap_or(0)
}

pub struct RayLights {
    pub buffer: wgpu::Buffer,
    /// Each listed block state's colour, level and material.
    table: Sources,
    /// The world corner and the sources' revision the buffer was last built for.
    built: Option<([i32; 3], u64)>,
}

impl RayLights {
    pub fn new(device: &wgpu::Device, table: Sources) -> Self {
        let buffer = device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("Native shader pack ray light sources"),
            size: u64::from(WORDS) * 4,
            usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST,
            mapped_at_creation: false,
        });
        Self {
            buffer,
            table,
            built: None,
        }
    }

    /// Places the grid around `camera`, rebuilding the sources and the cells when it moved or
    /// `revision` (of `sections`' sources) changed.
    pub fn update(
        &mut self,
        queue: &wgpu::Queue,
        camera: DVec3,
        sections: &FxHashMap<SectionKey, Vec<([i32; 3], u32)>>,
        revision: u64,
    ) {
        let origin = grid_origin(camera);
        if self.built != Some((origin, revision)) {
            self.built = Some((origin, revision));
            let words = build(&self.table, origin, camera, sections);
            queue.write_buffer(&self.buffer, 0, bytemuck::cast_slice(&words));
        }
        let corner = (IVec3::from(origin).as_dvec3() - camera).as_vec3();
        let corner = corner.to_array().map(f32::to_bits);
        queue.write_buffer(&self.buffer, 16, bytemuck::cast_slice(&corner));
    }
}

/// The grid's world corner for `camera`: on the 16-block steps, the camera near its middle.
fn grid_origin(camera: DVec3) -> [i32; 3] {
    let half = f64::from(RAY_LIGHT_GRID) * f64::from(RAY_LIGHT_CELL) / 2.0;
    ((camera - half) / f64::from(STEP))
        .floor()
        .as_ivec3()
        .to_array()
        .map(|v| v * STEP)
}

/// The buffer's words for the grid with its corner at `origin` (the corner itself left 0: the
/// frame writes it).
fn build(
    table: &Sources,
    origin: [i32; 3],
    camera: DVec3,
    sections: &FxHashMap<SectionKey, Vec<([i32; 3], u32)>>,
) -> Vec<u32> {
    let n = RAY_LIGHT_GRID as usize;
    let mut words = vec![0u32; WORDS as usize];
    words[1] = RAY_LIGHT_GRID;
    words[2] = RAY_LIGHT_CELL.to_bits();
    words[3] = RAY_LIGHT_PER_CELL;
    words[8] = COUNTS;
    words[9] = LISTS;
    let span = RAY_LIGHT_GRID as i32 * RAY_LIGHT_CELL as i32;
    // Sections the grid or the light reaching into it (15 blocks) touches.
    let lo = origin.map(|v| (v - 15).div_euclid(16));
    let hi = origin.map(|v| (v + span + 15).div_euclid(16));
    // (position from the corner, colour, level, distance² to the camera, material)
    let mut sources: Vec<(Vec3, [f32; 3], f32, f64, u32)> = Vec::new();
    for (key, lights) in sections {
        let key = [key.0, key.1 + crate::chunk::MIN_Y.div_euclid(16), key.2];
        if (0..3).any(|i| key[i] < lo[i] || key[i] > hi[i]) {
            continue;
        }
        for &(pos, state) in lights {
            let Some(source) = table.get(&state) else {
                continue;
            };
            let center = IVec3::from(pos).as_dvec3() + 0.5;
            let local = (center - IVec3::from(origin).as_dvec3()).as_vec3();
            let d = center.distance_squared(camera);
            let level = f32::from(source.level);
            sources.push((local, source.color, level, d, source.material));
        }
    }
    let max = RAY_LIGHT_MAX as usize;
    if sources.len() > max {
        sources.select_nth_unstable_by(max, |a, b| a.3.total_cmp(&b.3));
        sources.truncate(max);
    }
    sources.sort_unstable_by(|a, b| a.3.total_cmp(&b.3));
    words[0] = sources.len() as u32;
    for (i, (at, color, level, _, material)) in sources.iter().enumerate() {
        let w = (RAY_LIGHT_HEADER + i as u32 * RAY_LIGHT_WORDS) as usize;
        let floats = [at.x, at.y, at.z, *level, color[0], color[1], color[2]];
        for (k, f) in floats.into_iter().enumerate() {
            words[w + k] = f.to_bits();
        }
        words[w + 7] = *material;
    }
    // Each cell's sources: those whose light (as far as its level) reaches the cell's box,
    // the strongest at the cell's nearest point first.
    let mut cells: Vec<Vec<(f32, u32)>> = vec![Vec::new(); n * n * n];
    for (i, (at, _, reach, _, _)) in sources.iter().enumerate() {
        let first = ((*at - *reach) / RAY_LIGHT_CELL).floor().max(Vec3::ZERO);
        let last = ((*at + *reach) / RAY_LIGHT_CELL)
            .floor()
            .min(Vec3::splat(n as f32 - 1.0));
        if first.cmpgt(last).any() {
            continue;
        }
        let (first, last) = (first.as_ivec3(), last.as_ivec3());
        for z in first.z..=last.z {
            for y in first.y..=last.y {
                for x in first.x..=last.x {
                    let low = IVec3::new(x, y, z).as_vec3() * RAY_LIGHT_CELL;
                    let near = at.clamp(low, low + RAY_LIGHT_CELL);
                    let left = reach - near.distance(*at);
                    if left > 0.0 {
                        cells[x as usize + n * (y as usize + n * z as usize)]
                            .push((left, i as u32));
                    }
                }
            }
        }
    }
    let per = RAY_LIGHT_PER_CELL as usize;
    for (c, list) in cells.iter_mut().enumerate() {
        list.sort_unstable_by(|a, b| b.0.total_cmp(&a.0));
        list.truncate(per);
        words[COUNTS as usize + c] = list.len() as u32;
        for (k, &(_, i)) in list.iter().enumerate() {
            words[LISTS as usize + c * per + k] = i;
        }
    }
    words
}

#[cfg(test)]
mod tests {
    use super::*;

    fn lit(color: [f32; 3], level: u8, material: u32) -> Source {
        Source {
            color,
            level,
            material,
        }
    }

    /// `(state, block)` sources keyed by their sections, as the meshes list them.
    fn sections(blocks: &[([i32; 3], u32)]) -> FxHashMap<SectionKey, Vec<([i32; 3], u32)>> {
        let mut sections: FxHashMap<SectionKey, Vec<_>> = FxHashMap::default();
        for &(p, state) in blocks {
            let key = (p[0] >> 4, (p[1] - crate::chunk::MIN_Y) >> 4, p[2] >> 4);
            sections.entry(key).or_default().push((p, state));
        }
        sections
    }

    /// A torch at the camera and a dim source in its cell: both are listed nearest first, with
    /// their colour, level and material, a cell the torch's light still reaches lists only the
    /// torch, one past it nothing.
    #[test]
    fn the_cells_list_the_sources_that_reach_them() {
        let mut table = Sources::default();
        table.insert(7, lit([2.0, 1.0, 0.0], 14, 5));
        table.insert(9, lit([1.0, 1.0, 1.0], 3, 0));
        let sections = sections(&[([8, 64, 8], 7), ([12, 64, 12], 9), ([5, 64, 5], 1)]);
        let camera = DVec3::new(8.5, 64.5, 8.5);
        let origin = grid_origin(camera);
        assert_eq!(origin, [-64, 0, -64]);
        let words = build(&table, origin, camera, &sections);
        let float = |i: usize| f32::from_bits(words[i]);
        // State 1 gives no light: two sources, the torch first.
        assert_eq!(words[0], 2);
        let torch = RAY_LIGHT_HEADER as usize;
        assert_eq!(
            [float(torch), float(torch + 1), float(torch + 2)],
            [72.5, 64.5, 72.5]
        );
        assert_eq!(float(torch + 3), 14.0);
        assert_eq!(float(torch + 4), 2.0);
        assert_eq!(words[torch + 7], 5);
        assert_eq!(words[torch + RAY_LIGHT_WORDS as usize + 7], 0);
        let cell = |p: [i32; 3]| {
            let c = p.map(|v| v as usize / RAY_LIGHT_CELL as usize);
            c[0] + RAY_LIGHT_GRID as usize * (c[1] + RAY_LIGHT_GRID as usize * c[2])
        };
        let listed = |p: [i32; 3]| {
            let c = cell(p);
            let count = words[COUNTS as usize + c] as usize;
            let at = LISTS as usize + c * RAY_LIGHT_PER_CELL as usize;
            words[at..at + count].to_vec()
        };
        // The torch's cell: the torch (all 14 levels left) before the dim source.
        assert_eq!(listed([72, 64, 72]), vec![0, 1]);
        // The cell from x 80: 7.5 blocks from the torch, 3.5 from the dim source (level 3).
        assert_eq!(listed([85, 64, 72]), vec![0]);
        assert_eq!(listed([100, 64, 72]), Vec::<u32>::new());
    }

    /// Lava is left out, a lantern under water is listed; a block's material is the first of
    /// its faces' sprite layers that has one, 0 where none does; a block without light is none.
    #[test]
    fn a_source_is_a_block_s_own_light_with_its_faces_material() {
        let materials = [(3, 40), (8, 12)];
        let orange = Some([1.0, 0.5, 0.1]);
        assert_eq!(source((15, Fluid::Lava), orange, [9], &materials), None);
        assert_eq!(
            source((15, Fluid::Water), orange, [2, 8, 3], &materials),
            Some(lit([1.0, 0.5, 0.1], 15, 12))
        );
        assert_eq!(
            source((14, Fluid::None), None, [1, 2], &materials),
            Some(lit([1.0; 3], 14, 0))
        );
        assert_eq!(source((0, Fluid::None), orange, [3], &materials), None);
    }

    /// The CPU cost of one rebuild and its upload, in release (`--release -- --ignored
    /// --nocapture`): 1024 sources spread over the grid, 1024 packed in 16×4×16 blocks at the
    /// camera, and none.
    #[test]
    #[ignore = "timing"]
    fn rebuild_timings() {
        let mut table = Sources::default();
        table.insert(1, lit([1.0; 3], 15, 1));
        let camera = DVec3::new(0.5, 64.5, 0.5);
        let spread: Vec<_> = (0..16)
            .flat_map(|x| (0..4).flat_map(move |y| (0..16).map(move |z| (x, y, z))))
            .map(|(x, y, z)| ([x * 8 - 64, y * 32 + 2, z * 8 - 64], 1))
            .collect();
        let packed: Vec<_> = (0..16)
            .flat_map(|x| (0..4).flat_map(move |y| (0..16).map(move |z| (x, y, z))))
            .map(|(x, y, z)| ([x - 8, y + 62, z - 8], 1))
            .collect();
        let device = crate::gfx::test_gpu::optional_device();
        for (name, blocks) in [("spread", spread), ("packed", packed), ("none", Vec::new())] {
            let sections = sections(&blocks);
            let origin = grid_origin(camera);
            let runs: u32 = 200;
            let start = std::time::Instant::now();
            let mut words = Vec::new();
            for _ in 0..runs {
                words = build(&table, origin, camera, &sections);
            }
            let built = start.elapsed().as_secs_f64() * 1e6 / f64::from(runs);
            assert_eq!(words[0] as usize, blocks.len());
            let mut line = format!("{name}: build {built:.0} µs, {} bytes", words.len() * 4);
            if let Some((_, device, queue)) = &device {
                let mut lights = RayLights::new(device, table.clone());
                let start = std::time::Instant::now();
                for revision in 0..runs {
                    lights.update(queue, camera, &sections, u64::from(revision));
                    queue.submit([]);
                }
                let updated = start.elapsed().as_secs_f64() * 1e6 / f64::from(runs);
                line += &format!(", rebuild + upload {updated:.0} µs a frame");
            }
            eprintln!("{line}");
        }
    }
}
