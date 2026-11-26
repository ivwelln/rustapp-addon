const logo = document.querySelector("#rustapp-logo");
const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
const reflectionSpeed = () => motion.matches ? 0 : 0.18;

async function animateLogo() {
  if (!logo || !navigator.gpu) return;

  let shader;
  try {
    const { createShader } = await import("./logo-shaders.js");
    shader = await createShader(logo.querySelector("canvas"), {
      components: [
        {
          id: "rustapp-metal",
          type: "Chrome",
          props: {
            shapeSdfUrl: new URL("./rustapp-mark.sdf", import.meta.url).href,
            tint: "#f4f7f4",
            warmColor: "#d6ee91",
            coolColor: "#8daac2",
            bevelWidth: 0.014,
            bevelShape: 0.75,
            curvature: 0.34,
            waviness: 0.06,
            environment: 0.63,
            envRotation: 26,
            softness: 0.5,
            spectral: 0.52,
            dispersion: 0.09,
            shadows: 0.45,
            speed: reflectionSpeed(),
          },
        },
        { type: "Glow", props: { intensity: 0.2, threshold: 0.78, size: 8 } },
      ],
    }, {
      disableTelemetry: true,
      onReady() {
        logo.dataset.renderer = "webgpu";
      },
      onError() {
        if (shader?.getFailureReason()) delete logo.dataset.renderer;
      },
    });
    if (shader.getFailureReason()) {
      delete logo.dataset.renderer;
      return;
    }
    shader.update("rustapp-metal", { speed: reflectionSpeed() });
    motion.addEventListener("change", () => {
      shader.update("rustapp-metal", { speed: reflectionSpeed() });
    });
  } catch {
    delete logo.dataset.renderer;
  }
}

animateLogo();
