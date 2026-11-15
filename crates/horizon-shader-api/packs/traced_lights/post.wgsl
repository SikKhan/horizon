// Light from the blocks that give it off (torches, lanterns, glowstone, lava), each source tested
// with a ray, so walls stop it and corners cast shadows. It adds to the picture, whose own block
// light stays: it shows how the light sources are read, not how block light should look.

// How bright a source's light is at its block, over the picture's own colour.
const GAIN: f32 = 1.5;
// The most sources a pixel tests, of those its cell lists (the strongest there first).
const MOST: u32 = 8u;

// The face a pixel's point lies on, from its neighbours' points, turned toward the camera.
fn surface_normal(uv: vec2<f32>, position: vec3<f32>) -> vec3<f32> {
    let texel = 1.0 / vec2<f32>(textureDimensions(scene_depth));
    let right = uv + vec2<f32>(texel.x, 0.0);
    let down = uv + vec2<f32>(0.0, texel.y);
    let along = view_position(right, depth_at(right)) - position;
    let across = view_position(down, depth_at(down)) - position;
    var normal = normalize(cross(along, across));
    if dot(normal, position) > 0.0 {
        normal = -normal;
    }
    return normal;
}

fn post(input: PostInput) -> vec4<f32> {
    // The sky, and a device that doesn't trace: the picture as it is (no sources are listed then).
    if frame.has_ray_tracing == 0u || input.depth == 0.0 {
        return input.color;
    }
    let position = view_position(input.uv, input.depth);
    let normal = surface_normal(input.uv, position);
    // The rays start a little off the surface, which they would otherwise hit at once.
    let origin = position + normal * 0.02;
    let near = ray_lights_near(position);
    var light = vec3<f32>(0.0);
    for (var i = 0u; i < min(near.y, MOST); i++) {
        let source = ray_light(ray_light_at(near.x + i));
        let to = source.position - origin;
        let distance = length(to);
        if distance >= source.level {
            continue;
        }
        let direction = to / distance;
        let facing = dot(normal, direction);
        if facing <= 0.0 {
            continue;
        }
        // The ray stops short of the source's own block, which may be a solid one (glowstone).
        if trace_any(origin, direction, max(distance - 0.6, 0.0)) {
            continue;
        }
        // As block light falls off: strongest at the source, gone at its level.
        let falloff = 1.0 - distance / source.level;
        light += source.color * falloff * falloff * facing;
    }
    return vec4<f32>(input.color.rgb * (1.0 + light * GAIN), input.color.a);
}
