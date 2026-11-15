# Traced Lights

The smallest pack that reads the scene's light sources ([shaders.md](../../../../docs/shaders.md#light-sources),
[shaders-reference-0.2.md](../../../../docs/api/shaders-reference-0.2.md)): one post pass that finds each pixel's
point with `view_position` and its face from the neighbouring points, asks `ray_lights_near` which sources reach
it, and for each of the first eight, read with `ray_light`, tests with `trace_any` whether terrain stands between
them. Lit sources add their colour (white unless the pack's `[[lights]]` give one), fading out over their light
level. It shows the calls, not a look: the picture's own block light stays, edges against the sky take wrong
normals, and there is no bounce or denoising. Where the game doesn't trace (`frame.has_ray_tracing` is 0) the
picture stays as it is. 0.2 is experimental, so this is a test pack of the SDK: the game draws it, and the registry
doesn't list it yet. Check it with `horizon-shader check crates/horizon-shader-api/packs/traced_lights`.
