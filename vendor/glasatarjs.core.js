var __defProp = Object.defineProperty;
var __defProps = Object.defineProperties;
var __getOwnPropDescs = Object.getOwnPropertyDescriptors;
var __getOwnPropSymbols = Object.getOwnPropertySymbols;
var __hasOwnProp = Object.prototype.hasOwnProperty;
var __propIsEnum = Object.prototype.propertyIsEnumerable;
var __defNormalProp = (obj, key, value) =>
  key in obj
    ? __defProp(obj, key, {
        enumerable: true,
        configurable: true,
        writable: true,
        value,
      })
    : (obj[key] = value);
var __spreadValues = (a, b) => {
  for (var prop in b || (b = {}))
    if (__hasOwnProp.call(b, prop)) __defNormalProp(a, prop, b[prop]);
  if (__getOwnPropSymbols)
    for (var prop of __getOwnPropSymbols(b)) {
      if (__propIsEnum.call(b, prop)) __defNormalProp(a, prop, b[prop]);
    }
  return a;
};
var __spreadProps = (a, b) => __defProps(a, __getOwnPropDescs(b));

// src/lib/shaders.ts
var vertexShaderSource = `
  attribute vec2 a_position;
  attribute vec2 a_texCoord;
  varying vec2 v_texCoord;
  
  void main() {
    gl_Position = vec4(a_position, 0.0, 1.0);
    v_texCoord = a_texCoord;
  }
`;
var fragmentShaderSource = `
  precision highp float;
  
  uniform sampler2D u_backgroundTexture;
  uniform vec2 u_resolution;
  uniform float u_opacity;
  uniform float u_refraction;
  uniform int u_texture;
  uniform float u_blurAmount;
  
  varying vec2 v_texCoord;
  
  // Improved noise for glass patterns
  vec3 mod289(vec3 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
  vec4 mod289(vec4 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
  vec4 permute(vec4 x) { return mod289(((x*34.0)+1.0)*x); }
  vec4 taylorInvSqrt(vec4 r) { return 1.79284291400159 - 0.85373472095314 * r; }
  
  float snoise(vec3 v) {
    const vec2 C = vec2(1.0/6.0, 1.0/3.0);
    const vec4 D = vec4(0.0, 0.5, 1.0, 2.0);
    
    vec3 i = floor(v + dot(v, C.yyy));
    vec3 x0 = v - i + dot(i, C.xxx);
    
    vec3 g = step(x0.yzx, x0.xyz);
    vec3 l = 1.0 - g;
    vec3 i1 = min(g.xyz, l.zxy);
    vec3 i2 = max(g.xyz, l.zxy);
    
    vec3 x1 = x0 - i1 + C.xxx;
    vec3 x2 = x0 - i2 + C.yyy;
    vec3 x3 = x0 - D.yyy;
    
    i = mod289(i);
    vec4 p = permute(permute(permute(
             i.z + vec4(0.0, i1.z, i2.z, 1.0))
           + i.y + vec4(0.0, i1.y, i2.y, 1.0))
           + i.x + vec4(0.0, i1.x, i2.x, 1.0));
           
    float n_ = 0.142857142857;
    vec3 ns = n_ * D.wyz - D.xzx;
    
    vec4 j = p - 49.0 * floor(p * ns.z * ns.z);
    
    vec4 x_ = floor(j * ns.z);
    vec4 y_ = floor(j - 7.0 * x_);
    
    vec4 x = x_ *ns.x + ns.yyyy;
    vec4 y = y_ *ns.x + ns.yyyy;
    vec4 h = 1.0 - abs(x) - abs(y);
    
    vec4 b0 = vec4(x.xy, y.xy);
    vec4 b1 = vec4(x.zw, y.zw);
    
    vec4 s0 = floor(b0)*2.0 + 1.0;
    vec4 s1 = floor(b1)*2.0 + 1.0;
    vec4 sh = -step(h, vec4(0.0));
    
    vec4 a0 = b0.xzyw + s0.xzyw*sh.xxyy;
    vec4 a1 = b1.xzyw + s1.xzyw*sh.zzww;
    
    vec3 p0 = vec3(a0.xy,h.x);
    vec3 p1 = vec3(a0.zw,h.y);
    vec3 p2 = vec3(a1.xy,h.z);
    vec3 p3 = vec3(a1.zw,h.w);
    
    vec4 norm = taylorInvSqrt(vec4(dot(p0,p0), dot(p1,p1), dot(p2,p2), dot(p3,p3)));
    p0 *= norm.x;
    p1 *= norm.y;
    p2 *= norm.z;
    p3 *= norm.w;
    
    vec4 m = max(0.6 - vec4(dot(x0,x0), dot(x1,x1), dot(x2,x2), dot(x3,x3)), 0.0);
    m = m * m;
    return 42.0 * dot(m*m, vec4(dot(p0,x0), dot(p1,x1), dot(p2,x2), dot(p3,x3)));
  }
  
  // Optimized arctic glass - simplified crystalline pattern
  vec2 arcticGlass(vec2 uv) {
    float scale = 60.0;  // Reduced scale for performance
    vec2 distortion = vec2(0.0);
    
    // Reduced to 2 layers for better performance
    vec3 noiseCoord1 = vec3(uv * scale, 0.0);
    float noise1 = abs(snoise(noiseCoord1));
    
    vec3 noiseCoord2 = vec3(uv * scale * 0.5, 0.5);
    float noise2 = abs(snoise(noiseCoord2));
    
    // Combine layers more efficiently
    float combined = noise1 * 0.7 + noise2 * 0.3;
    distortion = vec2(
      combined * cos(noise1 * 6.28),
      combined * sin(noise1 * 6.28)
    ) * 0.006;
    
    return distortion;
  }
  
  // Static cathedral glass - flowing organic patterns
  vec2 cathedralGlass(vec2 uv) {
    vec2 distortion = vec2(0.0);
    
    // Large flowing patterns
    float flow1 = snoise(vec3(uv * 4.0, 0.0));
    float flow2 = snoise(vec3(uv * 6.0 + 100.0, 0.0));
    
    // Combine flows
    float combined = flow1 * 0.6 + flow2 * 0.4;
    
    distortion.x = combined * 0.02;
    distortion.y = snoise(vec3(uv * 5.0 + 50.0, 0.0)) * 0.02;
    
    return distortion;
  }
  
  // Optimized autumn glass - simplified organic patterns  
  vec2 autumnGlass(vec2 uv) {
    vec2 distortion = vec2(0.0);
    
    // Simplified organic structures (remove loop)
    vec3 p1 = vec3(uv * 8.0, 0.0);
    float n1 = snoise(p1);
    
    vec3 p2 = vec3(uv * 12.0 + vec2(37.0, 73.0), 0.0);
    float n2 = snoise(p2);
    
    // Create organic shapes more efficiently
    float organic = smoothstep(-0.2, 0.2, n1 * 0.7 + n2 * 0.3);
    distortion = vec2(
      organic * cos(n1 * 3.14),
      organic * sin(n1 * 3.14)
    ) * 0.012;
    
    return distortion;
  }
  
  // Static flemish glass - hammered texture
  vec2 flemishGlass(vec2 uv) {
    vec2 distortion = vec2(0.0);
    
    // Hammered dimples pattern
    vec2 cellSize = vec2(0.03, 0.03);
    vec2 cell = floor(uv / cellSize);
    vec2 localUV = fract(uv / cellSize) - 0.5;
    
    // Random offset per cell
    float cellHash = fract(sin(dot(cell, vec2(12.9898, 78.233))) * 43758.5453);
    vec2 cellOffset = vec2(
      sin(cellHash * 6.28) * 0.1,
      cos(cellHash * 6.28) * 0.1
    );
    
    // Dimple shape
    float dimple = 1.0 - smoothstep(0.0, 0.4, length(localUV + cellOffset));
    
    // Calculate normal-like distortion
    vec2 dimpleGrad = normalize(localUV + cellOffset) * dimple;
    distortion = dimpleGrad * 0.015;
    
    return distortion;
  }
  
  // Static ripple glass - water ripple pattern
  vec2 rippleGlass(vec2 uv) {
    vec2 distortion = vec2(0.0);
    
    // Concentric ripples from multiple centers
    vec2 center1 = vec2(0.3, 0.7);
    vec2 center2 = vec2(0.7, 0.2);
    vec2 center3 = vec2(0.1, 0.1);
    
    float ripple1 = sin(distance(uv, center1) * 40.0) * 0.5 + 0.5;
    float ripple2 = sin(distance(uv, center2) * 35.0) * 0.3 + 0.7;
    float ripple3 = sin(distance(uv, center3) * 60.0) * 0.2 + 0.8;
    
    float combined = ripple1 * ripple2 * ripple3;
    
    // Create gradient for distortion
    vec2 grad1 = normalize(uv - center1) * ripple1;
    vec2 grad2 = normalize(uv - center2) * ripple2;
    vec2 grad3 = normalize(uv - center3) * ripple3;
    
    distortion = (grad1 + grad2 + grad3) * 0.008 * combined;
    
    return distortion;
  }
  
  // Static reeded glass - vertical ridged pattern like in image
  vec2 reededGlass(vec2 uv) {
    vec2 distortion = vec2(0.0);
    
    // Vertical ridges with organic variation
    float ridgeSpacing = 0.08; // Distance between ridges
    float ridgePos = uv.x / ridgeSpacing;
    float ridgeIndex = floor(ridgePos);
    float ridgeLocal = fract(ridgePos) - 0.5;
    
    // Add organic variation to ridge position
    float ridgeVariation = snoise(vec3(ridgeIndex * 0.1, uv.y * 2.0, 0.0)) * 0.3;
    ridgeLocal += ridgeVariation;
    
    // Create curved ridge profile (cylindrical lens effect)
    float ridgeShape = 1.0 - abs(ridgeLocal * 2.0);
    ridgeShape = smoothstep(0.0, 1.0, ridgeShape);
    
    // Vertical flow variation
    float verticalFlow = snoise(vec3(uv.x * 8.0, uv.y * 3.0, 0.0)) * 0.2;
    
    // Primary horizontal distortion from ridges
    distortion.x = ridgeLocal * ridgeShape * 0.02;
    // Subtle vertical flow
    distortion.y = verticalFlow * ridgeShape * 0.008;
    
    return distortion;
  }
  
  // Static vintage glass - aged wavy pattern
  vec2 vintageGlass(vec2 uv) {
    vec2 distortion = vec2(0.0);
    
    // Vintage glass has characteristic waves and imperfections
    float wave1 = sin(uv.y * 15.0 + sin(uv.x * 8.0) * 2.0);
    float wave2 = sin(uv.x * 12.0 + sin(uv.y * 6.0) * 1.5);
    
    // Add some organic noise for imperfections
    float imperfection = snoise(vec3(uv * 20.0, 0.0));
    
    // Combine waves with slight randomness
    distortion.x = (wave1 * 0.4 + wave2 * 0.3 + imperfection * 0.3) * 0.01;
    distortion.y = (sin(uv.x * 18.0) * 0.5 + cos(uv.y * 14.0) * 0.5) * 0.008;
    
    return distortion;
  }
  
  // Optimized forest glass - simplified branching pattern
  vec2 forestGlass(vec2 uv) {
    vec2 distortion = vec2(0.0);
    
    // Create branching tree-like patterns
    vec2 center = uv - 0.5;
    float radius = length(center);
    
    // Simplified to 2 main branches for performance
    // Branch 1
    float branchAngle1 = snoise(vec3(uv * 4.0, 0.0)) * 0.5;
    vec2 branchDir1 = vec2(cos(branchAngle1), sin(branchAngle1));
    float branchDist1 = abs(dot(center, vec2(-branchDir1.y, branchDir1.x)));
    float thickness1 = 0.02 + radius * 0.03;
    float branch1 = (1.0 - smoothstep(0.0, thickness1, branchDist1)) * 
                    smoothstep(0.0, 0.1, radius) * (1.0 - smoothstep(0.3, 0.5, radius));
    
    // Branch 2  
    float branchAngle2 = 2.094 + snoise(vec3(uv * 4.0, 1.0)) * 0.5;
    vec2 branchDir2 = vec2(cos(branchAngle2), sin(branchAngle2));
    float branchDist2 = abs(dot(center, vec2(-branchDir2.y, branchDir2.x)));
    float branch2 = (1.0 - smoothstep(0.0, thickness1, branchDist2)) * 
                    smoothstep(0.0, 0.1, radius) * (1.0 - smoothstep(0.3, 0.5, radius));
    
    // Combine branches
    distortion = normalize(vec2(-branchDir1.y, branchDir1.x)) * branch1 * 0.012 +
                 normalize(vec2(-branchDir2.y, branchDir2.x)) * branch2 * 0.012;
    
    // Add subtle overall organic flow
    float globalFlow = snoise(vec3(uv * 8.0, 0.0));
    distortion += vec2(
      cos(globalFlow * 3.14159),
      sin(globalFlow * 3.14159)
    ) * 0.004;
    
    return distortion;
  }
  
  // Optimized 5-tap blur for better performance
  vec4 blur(sampler2D tex, vec2 uv, vec2 resolution, float blurSize) {
    vec4 color = vec4(0.0);
    float total = 0.0;
    
    // 5-tap cross blur (much faster than 25-tap)
    vec2 texelSize = blurSize / resolution;
    
    // Center
    float weight = 0.4;
    color += texture2D(tex, uv) * weight;
    total += weight;
    
    // Cross pattern
    weight = 0.15;
    color += texture2D(tex, uv + vec2(texelSize.x, 0.0)) * weight;
    color += texture2D(tex, uv - vec2(texelSize.x, 0.0)) * weight;
    color += texture2D(tex, uv + vec2(0.0, texelSize.y)) * weight;
    color += texture2D(tex, uv - vec2(0.0, texelSize.y)) * weight;
    total += weight * 4.0;
    
    return color / total;
  }
  
  void main() {
    vec2 uv = v_texCoord;
    
    // Get static glass distortion based on pattern
    // Use more efficient branching pattern
    vec2 glassDistortion = vec2(0.0);
    
    // Group similar patterns together to reduce branching
    if (u_texture < 4) {
      if (u_texture < 2) {
        glassDistortion = (u_texture == 0) ? arcticGlass(uv) : cathedralGlass(uv);
      } else {
        glassDistortion = (u_texture == 2) ? autumnGlass(uv) : flemishGlass(uv);
      }
    } else {
      if (u_texture < 6) {
        glassDistortion = (u_texture == 4) ? rippleGlass(uv) : reededGlass(uv);
      } else {
        glassDistortion = (u_texture == 6) ? vintageGlass(uv) : forestGlass(uv);
      }
    }
    
    // Apply refraction
    vec2 refractedUV = uv + glassDistortion * u_refraction;
    
    // Sample background with distortion and blur
    vec4 backgroundColor = blur(u_backgroundTexture, refractedUV, u_resolution, u_blurAmount);
    
    // Add subtle glass tint and highlights
    vec3 glassTint = vec3(0.95, 0.97, 1.0); // Slight blue-white tint
    vec3 finalColor = backgroundColor.rgb * glassTint;
    
    // Add surface highlights based on distortion
    float highlight = length(glassDistortion) * 10.0;
    highlight = smoothstep(0.0, 1.0, highlight) * 0.1;
    finalColor += vec3(highlight);
    
    // Apply opacity
    float finalAlpha = u_opacity;
    
    gl_FragColor = vec4(finalColor, finalAlpha);
  }
`;

// src/lib/audio-analyzer.ts
var AudioAnalyzer = class {
  constructor() {
    this.source = null;
    this.smoothingFactor = 0.85;
    this.audioContext = new (window.AudioContext ||
      window.webkitAudioContext)();
    this.analyser = this.audioContext.createAnalyser();
    this.analyser.fftSize = 512;
    this.analyser.smoothingTimeConstant = 0.1;
    const bufferLength = this.analyser.frequencyBinCount;
    this.dataArray = new Uint8Array(new ArrayBuffer(bufferLength));
    this.frequencyData = new Uint8Array(new ArrayBuffer(bufferLength));
  }
  connectStream(stream) {
    if (this.source) {
      this.source.disconnect();
    }
    this.source = this.audioContext.createMediaStreamSource(stream);
    this.source.connect(this.analyser);
  }
  getAudioData() {
    this.analyser.getByteTimeDomainData(this.dataArray);
    let sum = 0;
    const samples = Math.min(this.dataArray.length, 1024);
    for (let i = 0; i < samples; i++) {
      const amplitude = (this.dataArray[i] - 128) / 128;
      sum += amplitude * amplitude;
    }
    const rms = Math.sqrt(sum / samples);
    const audioLevel = Math.min(rms * 2, 1);
    return {
      level: audioLevel,
      frequency: 0,
      // Skip expensive calculation
      frequencyData: new Uint8Array(0),
      // Empty array to save memory
      timeDomainData: this.dataArray,
    };
  }
  disconnect() {
    if (this.source) {
      this.source.disconnect();
      this.source = null;
    }
  }
  dispose() {
    this.disconnect();
    if (this.audioContext.state !== 'closed') {
      this.audioContext.close();
    }
  }
};

// src/lib/privacy-glass-renderer.ts
var _GlastarJS = class _GlastarJS {
  constructor(canvas, config = {}) {
    this.program = null;
    this.audioAnalyzer = null;
    this.animationId = null;
    this.isDisposed = false;
    this.positionBuffer = null;
    this.texCoordBuffer = null;
    this.backgroundTexture = null;
    this.smoothedAudioLevel = 0;
    this.backgroundImageElement = null;
    this.startTime = Date.now();
    this.backgroundDirty = true;
    this.lastBackgroundConfig = '';
    this.textureNeedsUpdate = true;
    this.lastAvatarSize = 0;
    this.thinkingAnimationTime = 0;
    this.currentState = 'speaking';
    this.targetState = 'speaking';
    this.stateTransitionProgress = 1;
    // 0 to 1, 1 means fully transitioned
    this.stateTransitionDuration = 500;
    // milliseconds
    this.stateTransitionStartTime = 0;
    this.render = () => {
      if (!this.program || this.isDisposed) {
        return;
      }
      let audioData = {
        level: 0,
        frequency: 0,
        frequencyData: new Uint8Array(0),
        timeDomainData: new Uint8Array(0),
      };
      if (this.audioAnalyzer) {
        audioData = this.audioAnalyzer.getAudioData();
      }
      this.drawAvatar(audioData.level);
      if (this.textureNeedsUpdate) {
        this.gl.bindTexture(this.gl.TEXTURE_2D, this.backgroundTexture);
        this.gl.texImage2D(
          this.gl.TEXTURE_2D,
          0,
          this.gl.RGBA,
          this.gl.RGBA,
          this.gl.UNSIGNED_BYTE,
          this.backgroundCanvas
        );
        this.textureNeedsUpdate = false;
      }
      this.gl.clearColor(0, 0, 0, 0);
      this.gl.clear(this.gl.COLOR_BUFFER_BIT);
      this.gl.enable(this.gl.BLEND);
      this.gl.blendFunc(this.gl.SRC_ALPHA, this.gl.ONE_MINUS_SRC_ALPHA);
      this.gl.useProgram(this.program);
      this.gl.uniform1i(this.uniforms.backgroundTexture, 0);
      this.gl.uniform2f(
        this.uniforms.resolution,
        this.canvas.width,
        this.canvas.height
      );
      this.gl.uniform1f(this.uniforms.opacity, this.config.glassOpacity);
      this.gl.uniform1f(
        this.uniforms.refraction,
        this.config.refractionStrength
      );
      this.gl.uniform1i(
        this.uniforms.texture,
        _GlastarJS.TEXTURE_MAP[this.config.texture]
      );
      this.gl.uniform1f(this.uniforms.blurAmount, this.config.blurAmount);
      this.gl.activeTexture(this.gl.TEXTURE0);
      this.gl.bindTexture(this.gl.TEXTURE_2D, this.backgroundTexture);
      const positionLocation = this.gl.getAttribLocation(
        this.program,
        'a_position'
      );
      const texCoordLocation = this.gl.getAttribLocation(
        this.program,
        'a_texCoord'
      );
      this.gl.bindBuffer(this.gl.ARRAY_BUFFER, this.positionBuffer);
      this.gl.enableVertexAttribArray(positionLocation);
      this.gl.vertexAttribPointer(
        positionLocation,
        2,
        this.gl.FLOAT,
        false,
        0,
        0
      );
      this.gl.bindBuffer(this.gl.ARRAY_BUFFER, this.texCoordBuffer);
      this.gl.enableVertexAttribArray(texCoordLocation);
      this.gl.vertexAttribPointer(
        texCoordLocation,
        2,
        this.gl.FLOAT,
        false,
        0,
        0
      );
      this.gl.drawArrays(this.gl.TRIANGLE_STRIP, 0, 4);
      if (!this.isDisposed) {
        this.animationId = requestAnimationFrame(this.render);
      }
    };
    var _a, _b, _c, _d, _e, _f, _g, _h, _i, _j, _k, _l, _m;
    this.canvas = canvas;
    const width = config.width || 800;
    const height =
      config.avatarShape === 'circle' || config.avatarShape === 'square'
        ? width
        : config.height || 600;
    this.config = {
      width,
      height,
      texture: config.texture || 'arctic',
      glassOpacity: config.glassOpacity || 0.95,
      refractionStrength: config.refractionStrength || 1,
      blurAmount: config.blurAmount || 3,
      fps: config.fps || 60,
      avatarColor: config.avatarColor || '#4A90E2',
      avatarSize: config.avatarSize || 80,
      avatarSensitivity: config.avatarSensitivity || 1,
      avatarExpansion: config.avatarExpansion || 2,
      avatarSmoothing: config.avatarSmoothing || 0.25,
      avatarFadeWithAudio: config.avatarFadeWithAudio || false,
      avatarState: config.avatarState || 'speaking',
      avatarShape: config.avatarShape || 'square',
      listeningPulseBase: (_a = config.listeningPulseBase) != null ? _a : 50,
      listeningPulseAmplitude:
        (_b = config.listeningPulseAmplitude) != null ? _b : 35,
      listeningPulseSpeed:
        (_c = config.listeningPulseSpeed) != null ? _c : 2e-3,
      thinkingBorderWidth: (_d = config.thinkingBorderWidth) != null ? _d : 6,
      thinkingBorderSpeed: (_e = config.thinkingBorderSpeed) != null ? _e : 0.8,
      thinkingBorderLength:
        (_f = config.thinkingBorderLength) != null ? _f : 0.15,
      thinkingBorderTrailSegments:
        (_g = config.thinkingBorderTrailSegments) != null ? _g : 10,
      backgroundColor: config.backgroundColor || '#000000',
      backgroundType: config.backgroundType || 'color',
      backgroundGradient: {
        centerColor:
          ((_h = config.backgroundGradient) == null
            ? void 0
            : _h.centerColor) || '#4A90E2',
        edgeColor:
          ((_i = config.backgroundGradient) == null ? void 0 : _i.edgeColor) ||
          '#1a1a2e',
        angle:
          (_k = (_j = config.backgroundGradient) == null ? void 0 : _j.angle) !=
          null
            ? _k
            : 45,
      },
      backgroundImage: config.backgroundImage,
      backgroundRotation: (_l = config.backgroundRotation) != null ? _l : true,
      // If backgroundRotation is false, we set the rotation speed to 0 regardless of the backgroundRotationSpeed
      backgroundRotationSpeed:
        config.backgroundRotation === false
          ? 0
          : (_m = config.backgroundRotationSpeed) != null
            ? _m
            : 10,
      backgroundScale: config.backgroundScale || 1,
    };
    const gl = canvas.getContext('webgl2') || canvas.getContext('webgl');
    if (!gl) {
      throw new Error('WebGL not supported');
    }
    this.gl = gl;
    this.uniforms = {
      backgroundTexture: null,
      resolution: null,
      opacity: null,
      refraction: null,
      texture: null,
      blurAmount: null,
    };
    this.backgroundCanvas = document.createElement('canvas');
    this.backgroundCanvas.width = this.config.width;
    this.backgroundCanvas.height = this.config.height;
    this.backgroundCtx = this.backgroundCanvas.getContext('2d');
    this.setupWebGL();
    this.createBackgroundTexture();
  }
  createShader(type, source) {
    const shader = this.gl.createShader(type);
    if (!shader) {
      return null;
    }
    this.gl.shaderSource(shader, source);
    this.gl.compileShader(shader);
    if (!this.gl.getShaderParameter(shader, this.gl.COMPILE_STATUS)) {
      console.error(
        'Shader compilation error:',
        this.gl.getShaderInfoLog(shader)
      );
      this.gl.deleteShader(shader);
      return null;
    }
    return shader;
  }
  setupWebGL() {
    const vertexShader = this.createShader(
      this.gl.VERTEX_SHADER,
      vertexShaderSource
    );
    const fragmentShader = this.createShader(
      this.gl.FRAGMENT_SHADER,
      fragmentShaderSource
    );
    if (!vertexShader || !fragmentShader) {
      throw new Error('Failed to create shaders');
    }
    this.program = this.gl.createProgram();
    if (!this.program) {
      throw new Error('Failed to create WebGL program');
    }
    this.gl.attachShader(this.program, vertexShader);
    this.gl.attachShader(this.program, fragmentShader);
    this.gl.linkProgram(this.program);
    if (!this.gl.getProgramParameter(this.program, this.gl.LINK_STATUS)) {
      throw new Error(
        'Program linking error: ' + this.gl.getProgramInfoLog(this.program)
      );
    }
    this.gl.deleteShader(vertexShader);
    this.gl.deleteShader(fragmentShader);
    const positions = new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]);
    const texCoords = new Float32Array([0, 1, 1, 1, 0, 0, 1, 0]);
    this.positionBuffer = this.gl.createBuffer();
    this.gl.bindBuffer(this.gl.ARRAY_BUFFER, this.positionBuffer);
    this.gl.bufferData(this.gl.ARRAY_BUFFER, positions, this.gl.STATIC_DRAW);
    this.texCoordBuffer = this.gl.createBuffer();
    this.gl.bindBuffer(this.gl.ARRAY_BUFFER, this.texCoordBuffer);
    this.gl.bufferData(this.gl.ARRAY_BUFFER, texCoords, this.gl.STATIC_DRAW);
    this.uniforms = {
      backgroundTexture: this.gl.getUniformLocation(
        this.program,
        'u_backgroundTexture'
      ),
      resolution: this.gl.getUniformLocation(this.program, 'u_resolution'),
      opacity: this.gl.getUniformLocation(this.program, 'u_opacity'),
      refraction: this.gl.getUniformLocation(this.program, 'u_refraction'),
      texture: this.gl.getUniformLocation(this.program, 'u_texture'),
      blurAmount: this.gl.getUniformLocation(this.program, 'u_blurAmount'),
    };
    this.resize(this.config.width, this.config.height);
  }
  createBackgroundTexture() {
    this.backgroundTexture = this.gl.createTexture();
    this.gl.bindTexture(this.gl.TEXTURE_2D, this.backgroundTexture);
    this.gl.texParameteri(
      this.gl.TEXTURE_2D,
      this.gl.TEXTURE_WRAP_S,
      this.gl.CLAMP_TO_EDGE
    );
    this.gl.texParameteri(
      this.gl.TEXTURE_2D,
      this.gl.TEXTURE_WRAP_T,
      this.gl.CLAMP_TO_EDGE
    );
    this.gl.texParameteri(
      this.gl.TEXTURE_2D,
      this.gl.TEXTURE_MIN_FILTER,
      this.gl.LINEAR
    );
    this.gl.texParameteri(
      this.gl.TEXTURE_2D,
      this.gl.TEXTURE_MAG_FILTER,
      this.gl.LINEAR
    );
  }
  drawAvatar(audioLevel) {
    const ctx = this.backgroundCtx;
    const width = this.backgroundCanvas.width;
    const height = this.backgroundCanvas.height;
    this.thinkingAnimationTime += 16;
    this.updateStateTransition();
    let effectiveAudioLevel = audioLevel;
    if (this.targetState === 'speaking') {
      effectiveAudioLevel = audioLevel;
    } else if (
      this.currentState === 'speaking' &&
      this.stateTransitionProgress < 1
    ) {
      effectiveAudioLevel *= 1 - this.stateTransitionProgress;
    } else {
      effectiveAudioLevel = 0;
    }
    const targetLevel = effectiveAudioLevel * this.config.avatarSensitivity;
    this.smoothedAudioLevel +=
      (targetLevel - this.smoothedAudioLevel) * this.config.avatarSmoothing;
    const currentBackgroundConfig = JSON.stringify({
      type: this.config.backgroundType,
      color: this.config.backgroundColor,
      gradient: this.config.backgroundGradient,
      image: this.config.backgroundImage,
      rotation: this.config.backgroundRotation,
      speed: this.config.backgroundRotationSpeed,
      scale: this.config.backgroundScale,
      state: this.config.avatarState,
      // Include state in background config for proper dirty tracking
    });
    if (
      this.backgroundDirty ||
      this.lastBackgroundConfig !== currentBackgroundConfig ||
      this.config.backgroundRotation ||
      this.config.avatarState === 'thinking'
    ) {
      this.drawBackground(ctx, width, height);
      this.backgroundDirty = false;
      this.lastBackgroundConfig = currentBackgroundConfig;
      this.textureNeedsUpdate = true;
    }
    const centerX = width / 2;
    const centerY = height / 2;
    const baseSize = this.config.avatarSize;
    if (this.stateTransitionProgress === 1) {
      this.drawAvatarState(
        ctx,
        centerX,
        centerY,
        baseSize,
        width,
        height,
        this.targetState,
        1
      );
    } else {
      ctx.save();
      if (this.currentState !== this.targetState) {
        const currentOpacity = 1 - this.stateTransitionProgress;
        ctx.globalAlpha = currentOpacity;
        this.drawAvatarState(
          ctx,
          centerX,
          centerY,
          baseSize,
          width,
          height,
          this.currentState,
          currentOpacity
        );
      }
      ctx.globalAlpha = this.stateTransitionProgress;
      this.drawAvatarState(
        ctx,
        centerX,
        centerY,
        baseSize,
        width,
        height,
        this.targetState,
        this.stateTransitionProgress
      );
      ctx.restore();
    }
  }
  drawAvatarState(
    ctx,
    centerX,
    centerY,
    baseSize,
    width,
    height,
    state,
    opacity
  ) {
    switch (state) {
      case 'speaking':
        this.drawSpeakingAvatar(ctx, centerX, centerY, baseSize, opacity);
        break;
      case 'listening':
        this.drawListeningAvatar(
          ctx,
          centerX,
          centerY,
          baseSize,
          width,
          height,
          opacity
        );
        break;
      case 'thinking':
        this.drawThinkingAvatar(
          ctx,
          centerX,
          centerY,
          baseSize,
          width,
          height,
          opacity
        );
        break;
    }
  }
  drawSpeakingAvatar(ctx, centerX, centerY, baseSize, transitionOpacity = 1) {
    const maxExpansionSize = baseSize * this.config.avatarExpansion;
    const expandedSize =
      baseSize + this.smoothedAudioLevel * (maxExpansionSize - baseSize);
    let avatarOpacity = 1;
    if (this.config.avatarFadeWithAudio) {
      const scaledLevel = this.smoothedAudioLevel * 8;
      avatarOpacity = Math.max(0, Math.min(1, Math.pow(scaledLevel, 0.3)));
    }
    this.drawAvatarCircle(
      ctx,
      centerX,
      centerY,
      expandedSize,
      avatarOpacity * transitionOpacity
    );
    if (
      Math.abs(expandedSize - this.lastAvatarSize) > 0.5 ||
      Math.abs(avatarOpacity - 1) > 0.01
    ) {
      this.textureNeedsUpdate = true;
    }
    this.lastAvatarSize = expandedSize;
  }
  drawListeningAvatar(
    ctx,
    centerX,
    centerY,
    baseSize,
    width,
    height,
    transitionOpacity = 1
  ) {
    this.drawListeningBorder(
      ctx,
      centerX,
      centerY,
      baseSize,
      width,
      height,
      transitionOpacity
    );
    this.textureNeedsUpdate = true;
  }
  drawThinkingAvatar(
    ctx,
    centerX,
    centerY,
    baseSize,
    width,
    height,
    transitionOpacity = 1
  ) {
    this.drawThinkingBorder(
      ctx,
      centerX,
      centerY,
      baseSize,
      width,
      height,
      transitionOpacity
    );
    this.textureNeedsUpdate = true;
  }
  drawAvatarCircle(ctx, centerX, centerY, size, opacity) {
    const gradient = ctx.createRadialGradient(
      centerX,
      centerY,
      0,
      centerX,
      centerY,
      size
    );
    const color = this.config.avatarColor;
    const centerOpacity = Math.round(opacity * 255)
      .toString(16)
      .padStart(2, '0');
    const edgeOpacity = Math.round(opacity * 128)
      .toString(16)
      .padStart(2, '0');
    gradient.addColorStop(0, color + centerOpacity);
    gradient.addColorStop(0.7, color + edgeOpacity);
    gradient.addColorStop(1, color + '00');
    ctx.fillStyle = gradient;
    ctx.beginPath();
    ctx.arc(centerX, centerY, size, 0, Math.PI * 2);
    ctx.fill();
  }
  drawThinkingBorder(
    ctx,
    centerX,
    centerY,
    _baseSize,
    width,
    height,
    transitionOpacity = 1
  ) {
    if (this.config.avatarShape === 'circle') {
      this.drawThinkingBorderCircle(
        ctx,
        centerX,
        centerY,
        width,
        height,
        transitionOpacity
      );
    } else {
      this.drawThinkingBorderSquare(ctx, width, height, transitionOpacity);
    }
  }
  drawThinkingBorderCircle(
    ctx,
    centerX,
    centerY,
    width,
    height,
    transitionOpacity = 1
  ) {
    const borderRadius = Math.min(width, height) * 0.48;
    const borderWidth = this.config.thinkingBorderWidth;
    const arcLength = Math.PI * (this.config.thinkingBorderLength * 2);
    const rotationSpeed = this.config.thinkingBorderSpeed * 2e-3;
    const rotation =
      (this.thinkingAnimationTime * rotationSpeed) % (Math.PI * 2);
    const startAngle = rotation;
    const endAngle = rotation + arcLength;
    const trailSegments = 8;
    for (let i = trailSegments; i >= 1; i--) {
      const trailOpacity = 0.8 - i * 0.08;
      const trailStart = startAngle - i * 0.08;
      const trailEnd = startAngle - (i - 1) * 0.08;
      ctx.strokeStyle =
        this.config.avatarColor +
        Math.round(trailOpacity * transitionOpacity * 255)
          .toString(16)
          .padStart(2, '0');
      ctx.lineWidth = borderWidth;
      ctx.lineCap = 'round';
      ctx.beginPath();
      ctx.arc(centerX, centerY, borderRadius, trailStart, trailEnd);
      ctx.stroke();
    }
    ctx.strokeStyle =
      this.config.avatarColor +
      Math.round(transitionOpacity * 255)
        .toString(16)
        .padStart(2, '0');
    ctx.lineWidth = borderWidth;
    ctx.lineCap = 'round';
    ctx.beginPath();
    ctx.arc(centerX, centerY, borderRadius, startAngle, endAngle);
    ctx.stroke();
    ctx.strokeStyle =
      this.config.avatarColor +
      Math.round(transitionOpacity * 255)
        .toString(16)
        .padStart(2, '0');
    ctx.lineWidth = borderWidth * 1.5;
    ctx.beginPath();
    ctx.arc(centerX, centerY, borderRadius, endAngle - 0.1, endAngle);
    ctx.stroke();
  }
  drawThinkingBorderSquare(ctx, width, height, transitionOpacity = 1) {
    const borderWidth = this.config.thinkingBorderWidth;
    const padding = 10;
    const rectWidth = width - padding * 2;
    const rectHeight = height - padding * 2;
    const perimeter = (rectWidth + rectHeight) * 2;
    const segmentLength = perimeter * this.config.thinkingBorderLength;
    const speed = this.config.thinkingBorderSpeed;
    const currentPosition = (this.thinkingAnimationTime * speed) % perimeter;
    const getPositionOnRect = pos => {
      let p = pos % perimeter;
      if (p < 0) {
        p += perimeter;
      }
      const left = padding;
      const right = width - padding;
      const top = padding;
      const bottom = height - padding;
      if (p < rectWidth) {
        return { x: left + p, y: top };
      }
      p -= rectWidth;
      if (p < rectHeight) {
        return { x: right, y: top + p };
      }
      p -= rectHeight;
      if (p < rectWidth) {
        return { x: right - p, y: bottom };
      }
      p -= rectWidth;
      return { x: left, y: bottom - p };
    };
    const trailSegments = this.config.thinkingBorderTrailSegments;
    const segmentStep = segmentLength / trailSegments;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    for (let i = trailSegments; i >= 0; i--) {
      const segmentStart = currentPosition - i * segmentStep;
      const segmentEnd = currentPosition - (i - 1) * segmentStep;
      const opacity = 1 - (i / trailSegments) * 0.7;
      ctx.strokeStyle =
        this.config.avatarColor +
        Math.round(opacity * transitionOpacity * 255)
          .toString(16)
          .padStart(2, '0');
      ctx.lineWidth = borderWidth * (1 + (1 - i / trailSegments) * 0.3);
      ctx.beginPath();
      const start = getPositionOnRect(segmentStart);
      ctx.moveTo(start.x, start.y);
      const steps = 5;
      for (let j = 1; j <= steps; j++) {
        const pos = segmentStart + (segmentEnd - segmentStart) * (j / steps);
        const point = getPositionOnRect(pos);
        ctx.lineTo(point.x, point.y);
      }
      ctx.stroke();
    }
    const headPos = getPositionOnRect(currentPosition);
    ctx.shadowBlur = 15;
    ctx.shadowColor = this.config.avatarColor;
    ctx.fillStyle = this.config.avatarColor;
    ctx.beginPath();
    ctx.arc(headPos.x, headPos.y, borderWidth / 2, 0, Math.PI * 2);
    ctx.fill();
    ctx.shadowBlur = 0;
  }
  drawListeningBorder(
    ctx,
    centerX,
    centerY,
    _baseSize,
    width,
    height,
    transitionOpacity = 1
  ) {
    if (this.config.avatarShape === 'circle') {
      this.drawListeningBorderCircle(
        ctx,
        centerX,
        centerY,
        width,
        height,
        transitionOpacity
      );
    } else {
      this.drawListeningBorderSquare(ctx, width, height, transitionOpacity);
    }
  }
  drawListeningBorderCircle(
    ctx,
    centerX,
    centerY,
    width,
    height,
    transitionOpacity = 1
  ) {
    var _a, _b, _c;
    const radius = Math.min(width, height) * 0.52;
    const pulseSpeed =
      (_a = this.config.listeningPulseSpeed) != null ? _a : 2e-3;
    const baseGlowWidth =
      (_b = this.config.listeningPulseBase) != null ? _b : 50;
    const pulseAmplitude =
      (_c = this.config.listeningPulseAmplitude) != null ? _c : 35;
    const pulsePhase = (Date.now() * pulseSpeed) % (Math.PI * 2);
    const glowWidth = baseGlowWidth + Math.sin(pulsePhase) * pulseAmplitude;
    ctx.save();
    const gradient = ctx.createRadialGradient(
      centerX,
      centerY,
      radius - glowWidth,
      // Inner radius (transparent)
      centerX,
      centerY,
      radius
      // Outer radius (colored)
    );
    gradient.addColorStop(0, 'rgba(0,0,0,0)');
    gradient.addColorStop(
      0.3,
      this.config.avatarColor +
        Math.round(0.1 * transitionOpacity * 255)
          .toString(16)
          .padStart(2, '0')
    );
    gradient.addColorStop(
      0.7,
      this.config.avatarColor +
        Math.round(0.4 * transitionOpacity * 255)
          .toString(16)
          .padStart(2, '0')
    );
    gradient.addColorStop(
      1,
      this.config.avatarColor +
        Math.round(0.8 * transitionOpacity * 255)
          .toString(16)
          .padStart(2, '0')
    );
    ctx.fillStyle = gradient;
    ctx.beginPath();
    ctx.arc(centerX, centerY, radius, 0, Math.PI * 2);
    ctx.fill();
    ctx.shadowBlur = 8;
    ctx.shadowColor = this.config.avatarColor;
    ctx.strokeStyle =
      this.config.avatarColor +
      Math.round(0.3 * transitionOpacity * 255)
        .toString(16)
        .padStart(2, '0');
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(centerX, centerY, radius - 1, 0, Math.PI * 2);
    ctx.stroke();
    ctx.shadowBlur = 0;
    ctx.restore();
  }
  drawListeningBorderSquare(ctx, width, height, transitionOpacity = 1) {
    var _a, _b, _c;
    const padding = -5;
    const pulseSpeed =
      (_a = this.config.listeningPulseSpeed) != null ? _a : 2e-3;
    const baseGlowWidth =
      (_b = this.config.listeningPulseBase) != null ? _b : 50;
    const pulseAmplitude =
      (_c = this.config.listeningPulseAmplitude) != null ? _c : 35;
    const pulsePhase = (Date.now() * pulseSpeed) % (Math.PI * 2);
    const glowWidth = baseGlowWidth + Math.sin(pulsePhase) * pulseAmplitude;
    const rectWidth = width - padding * 2;
    const rectHeight = height - padding * 2;
    const cornerRadius = 12;
    ctx.save();
    const layers = 8;
    for (let i = 0; i < layers; i++) {
      const layerProgress = i / (layers - 1);
      const inset = glowWidth * layerProgress;
      const layerOpacity = (1 - layerProgress) * 0.5 * transitionOpacity;
      ctx.strokeStyle =
        this.config.avatarColor +
        Math.round(layerOpacity * 255)
          .toString(16)
          .padStart(2, '0');
      ctx.lineWidth = 3;
      if (i < 3) {
        ctx.shadowBlur = 10 - i * 3;
        ctx.shadowColor = this.config.avatarColor;
      } else {
        ctx.shadowBlur = 0;
      }
      ctx.beginPath();
      ctx.roundRect(
        padding + inset,
        padding + inset,
        rectWidth - inset * 2,
        rectHeight - inset * 2,
        Math.max(1, cornerRadius - inset * 0.5)
      );
      ctx.stroke();
    }
    ctx.shadowBlur = 8;
    ctx.shadowColor = this.config.avatarColor;
    ctx.strokeStyle =
      this.config.avatarColor +
      Math.round(0.6 * transitionOpacity * 255)
        .toString(16)
        .padStart(2, '0');
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.roundRect(padding, padding, rectWidth, rectHeight, cornerRadius);
    ctx.stroke();
    ctx.shadowBlur = 0;
    ctx.restore();
  }
  updateStateTransition() {
    if (this.config.avatarState !== this.targetState) {
      this.currentState = this.targetState;
      this.targetState = this.config.avatarState;
      this.stateTransitionProgress = 0;
      this.stateTransitionStartTime = Date.now();
    }
    if (this.stateTransitionProgress < 1) {
      const elapsed = Date.now() - this.stateTransitionStartTime;
      this.stateTransitionProgress = Math.min(
        1,
        elapsed / this.stateTransitionDuration
      );
      this.stateTransitionProgress = this.easeInOutCubic(
        this.stateTransitionProgress
      );
      this.textureNeedsUpdate = true;
      if (this.stateTransitionProgress === 1) {
        this.currentState = this.targetState;
      }
    }
  }
  easeInOutCubic(t) {
    return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
  }
  drawBackground(ctx, width, height) {
    const centerX = width / 2;
    const centerY = height / 2;
    let rotationAngle = 0;
    if (this.config.backgroundRotation) {
      const elapsed = (Date.now() - this.startTime) / 1e3;
      rotationAngle =
        (elapsed * this.config.backgroundRotationSpeed * Math.PI) / 180;
    }
    ctx.save();
    ctx.translate(centerX, centerY);
    if (this.config.backgroundScale !== 1) {
      ctx.scale(this.config.backgroundScale, this.config.backgroundScale);
    }
    if (rotationAngle !== 0) {
      ctx.rotate(rotationAngle);
    }
    ctx.translate(-centerX, -centerY);
    switch (this.config.backgroundType) {
      case 'color':
        ctx.fillStyle = this.config.backgroundColor;
        ctx.fillRect(0, 0, width, height);
        break;
      case 'radial-gradient': {
        const radialGradient = ctx.createRadialGradient(
          centerX,
          centerY,
          0,
          centerX,
          centerY,
          (Math.max(width, height) / 2) * this.config.backgroundScale
        );
        radialGradient.addColorStop(
          0,
          this.config.backgroundGradient.centerColor
        );
        radialGradient.addColorStop(
          1,
          this.config.backgroundGradient.edgeColor
        );
        ctx.fillStyle = radialGradient;
        ctx.fillRect(0, 0, width, height);
        break;
      }
      case 'linear-gradient': {
        const angle =
          ((this.config.backgroundGradient.angle || 0) * Math.PI) / 180;
        const diagonal =
          (Math.sqrt(width * width + height * height) *
            this.config.backgroundScale) /
          2;
        const startX = centerX - Math.cos(angle) * diagonal;
        const startY = centerY - Math.sin(angle) * diagonal;
        const endX = centerX + Math.cos(angle) * diagonal;
        const endY = centerY + Math.sin(angle) * diagonal;
        const linearGradient = ctx.createLinearGradient(
          startX,
          startY,
          endX,
          endY
        );
        linearGradient.addColorStop(
          0,
          this.config.backgroundGradient.centerColor
        );
        linearGradient.addColorStop(
          1,
          this.config.backgroundGradient.edgeColor
        );
        ctx.fillStyle = linearGradient;
        ctx.fillRect(0, 0, width, height);
        break;
      }
      case 'image':
        if (
          this.backgroundImageElement &&
          this.backgroundImageElement.complete
        ) {
          const imgAspect =
            this.backgroundImageElement.width /
            this.backgroundImageElement.height;
          const canvasAspect = width / height;
          let drawWidth, drawHeight, drawX, drawY;
          if (imgAspect > canvasAspect) {
            drawHeight = height;
            drawWidth = height * imgAspect;
            drawX = (width - drawWidth) / 2;
            drawY = 0;
          } else {
            drawWidth = width;
            drawHeight = width / imgAspect;
            drawX = 0;
            drawY = (height - drawHeight) / 2;
          }
          ctx.drawImage(
            this.backgroundImageElement,
            drawX,
            drawY,
            drawWidth,
            drawHeight
          );
        } else {
          ctx.fillStyle = this.config.backgroundColor;
          ctx.fillRect(0, 0, width, height);
        }
        break;
    }
    ctx.restore();
  }
  loadBackgroundImage() {
    if (!this.config.backgroundImage) {
      if (this.backgroundImageElement) {
        this.backgroundImageElement.onload = null;
        this.backgroundImageElement.onerror = null;
        this.backgroundImageElement.src = '';
        this.backgroundImageElement = null;
      }
      return;
    }
    if (this.backgroundImageElement) {
      this.backgroundImageElement.onload = null;
      this.backgroundImageElement.onerror = null;
      this.backgroundImageElement.src = '';
    }
    this.backgroundImageElement = new Image();
    this.backgroundImageElement.crossOrigin = 'anonymous';
    this.backgroundImageElement.onload = () => {};
    this.backgroundImageElement.onerror = () => {
      console.warn(
        'Failed to load background image:',
        this.config.backgroundImage
      );
      if (this.backgroundImageElement) {
        this.backgroundImageElement.onload = null;
        this.backgroundImageElement.onerror = null;
        this.backgroundImageElement = null;
      }
    };
    this.backgroundImageElement.src = this.config.backgroundImage;
  }
  connectAudioStream(stream) {
    if (!this.audioAnalyzer) {
      this.audioAnalyzer = new AudioAnalyzer();
    }
    this.audioAnalyzer.connectStream(stream);
  }
  updateConfig(config) {
    var _a;
    this.config = __spreadProps(
      __spreadValues(__spreadValues({}, this.config), config),
      {
        // Handle nested backgroundGradient properly
        backgroundGradient: config.backgroundGradient
          ? {
              centerColor:
                config.backgroundGradient.centerColor ||
                this.config.backgroundGradient.centerColor,
              edgeColor:
                config.backgroundGradient.edgeColor ||
                this.config.backgroundGradient.edgeColor,
              angle:
                (_a = config.backgroundGradient.angle) != null
                  ? _a
                  : this.config.backgroundGradient.angle,
            }
          : this.config.backgroundGradient,
      }
    );
    const backgroundKeys = [
      'backgroundColor',
      'backgroundType',
      'backgroundGradient',
      'backgroundImage',
      'backgroundRotation',
      'backgroundRotationSpeed',
      'backgroundScale',
    ];
    if (
      backgroundKeys.some(key =>
        Object.prototype.hasOwnProperty.call(config, key)
      )
    ) {
      this.backgroundDirty = true;
    }
    if (config.width !== void 0 || config.height !== void 0) {
      this.resize(this.config.width, this.config.height);
      this.backgroundDirty = true;
    }
    if (config.backgroundImage !== void 0) {
      this.loadBackgroundImage();
    }
  }
  resize(width, height) {
    const effectiveHeight =
      this.config.avatarShape === 'circle' ||
      this.config.avatarShape === 'square'
        ? width
        : height || width;
    this.canvas.width = width;
    this.canvas.height = effectiveHeight;
    this.backgroundCanvas.width = width;
    this.backgroundCanvas.height = effectiveHeight;
    this.gl.viewport(0, 0, width, effectiveHeight);
    this.config.width = width;
    this.config.height = effectiveHeight;
    this.backgroundDirty = true;
  }
  start() {
    if (!this.animationId) {
      this.render();
    }
  }
  stop() {
    if (this.animationId) {
      cancelAnimationFrame(this.animationId);
      this.animationId = null;
    }
  }
  dispose() {
    this.isDisposed = true;
    this.stop();
    if (this.audioAnalyzer) {
      this.audioAnalyzer.dispose();
      this.audioAnalyzer = null;
    }
    if (this.program) {
      this.gl.deleteProgram(this.program);
      this.program = null;
    }
    if (this.positionBuffer) {
      this.gl.deleteBuffer(this.positionBuffer);
      this.positionBuffer = null;
    }
    if (this.texCoordBuffer) {
      this.gl.deleteBuffer(this.texCoordBuffer);
      this.texCoordBuffer = null;
    }
    if (this.backgroundTexture) {
      this.gl.deleteTexture(this.backgroundTexture);
      this.backgroundTexture = null;
    }
    if (this.backgroundImageElement) {
      this.backgroundImageElement.onload = null;
      this.backgroundImageElement.onerror = null;
      this.backgroundImageElement.src = '';
      this.backgroundImageElement = null;
    }
    if (this.backgroundCanvas) {
      this.backgroundCanvas.width = 0;
      this.backgroundCanvas.height = 0;
    }
  }
};
_GlastarJS.TEXTURE_MAP = {
  arctic: 0,
  cathedral: 1,
  autumn: 2,
  flemish: 3,
  ripple: 4,
  reeded: 5,
  vintage: 6,
  forest: 7,
};
var GlastarJS = _GlastarJS;
export { AudioAnalyzer, GlastarJS };
//# sourceMappingURL=core.js.map
