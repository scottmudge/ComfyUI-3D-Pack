import torch
import numpy as np
from PIL import Image
import pymeshlab
import pymeshlab as ml
from pymeshlab import PercentageValue
from pytorch3d.renderer import TexturesVertex
from pytorch3d.structures import Meshes
from rembg import new_session, remove
import torch
import torch.nn.functional as F
from typing import List, Tuple
from PIL import Image
import trimesh

providers = [
    ('CUDAExecutionProvider', {
        'device_id': 0,
        'arena_extend_strategy': 'kSameAsRequested',
        'gpu_mem_limit': 8 * 1024 * 1024 * 1024,
        'cudnn_conv_algo_search': 'HEURISTIC',
    })
]

session = new_session(providers=providers)

NEG_PROMPT="sketch, sculpture, hand drawing, outline, single color, NSFW, lowres, bad anatomy,bad hands, text, error, missing fingers, yellow sleeves, extra digit, fewer digits, cropped, worst quality, low quality, normal quality, jpeg artifacts, signature, watermark, username, blurry,(worst quality:1.4),(low quality:1.4)"

def load_mesh_with_trimesh(file_name, file_type=None):
    import trimesh
    mesh: trimesh.Trimesh = trimesh.load(file_name, file_type=file_type)
    if isinstance(mesh, trimesh.Scene):
        assert len(mesh.geometry) > 0
        # save to obj first and load again to avoid offset issue
        from io import BytesIO
        with BytesIO() as f:
            mesh.export(f, file_type="obj")
            f.seek(0)
            mesh = trimesh.load(f, file_type="obj")
        if isinstance(mesh, trimesh.Scene):
            # we lose texture information here
            mesh = trimesh.util.concatenate(
                tuple(trimesh.Trimesh(vertices=g.vertices, faces=g.faces)
                    for g in mesh.geometry.values()))
    assert isinstance(mesh, trimesh.Trimesh)

    vertices = torch.from_numpy(mesh.vertices).T
    faces = torch.from_numpy(mesh.faces).T
    colors = None
    if mesh.visual is not None:
        if hasattr(mesh.visual, 'vertex_colors'):
            colors = torch.from_numpy(mesh.visual.vertex_colors)[..., :3].T / 255.
    if colors is None:
        # print("Warning: no vertex color found in mesh! Filling it with gray.")
        colors = torch.ones_like(vertices) * 0.5
    return vertices, faces, colors

def meshlab_mesh_to_py3dmesh(mesh: pymeshlab.Mesh) -> Meshes:
    verts = torch.from_numpy(mesh.vertex_matrix()).float()
    faces = torch.from_numpy(mesh.face_matrix()).long()
    colors = torch.from_numpy(mesh.vertex_color_matrix()[..., :3]).float()
    textures = TexturesVertex(verts_features=[colors])
    return Meshes(verts=[verts], faces=[faces], textures=textures)


def py3dmesh_to_meshlab_mesh(meshes: Meshes) -> pymeshlab.Mesh:
    colors_in = F.pad(meshes.textures.verts_features_packed().cpu().float(), [0,1], value=1).numpy().astype(np.float64)
    m1 = pymeshlab.Mesh(
        vertex_matrix=meshes.verts_packed().cpu().float().numpy().astype(np.float64),
        face_matrix=meshes.faces_packed().cpu().long().numpy().astype(np.int32),
        v_normals_matrix=meshes.verts_normals_packed().cpu().float().numpy().astype(np.float64),
        v_color_matrix=colors_in)
    return m1


def to_pyml_mesh(vertices,faces):
    m1 = pymeshlab.Mesh(
        vertex_matrix=vertices.cpu().float().numpy().astype(np.float64),
        face_matrix=faces.cpu().long().numpy().astype(np.int32),
    )
    return m1


def to_py3d_mesh(vertices, faces, normals=None, vertices_color=None):
    from pytorch3d.structures import Meshes
    from pytorch3d.renderer.mesh.textures import TexturesVertex
    mesh = Meshes(verts=[vertices], faces=[faces], textures=None)

    if vertices_color is None:
        # set normals as vertext colors
        if normals is None:
            normals = mesh.verts_normals_packed()
        mesh.textures = TexturesVertex(verts_features=[normals / 2 + 0.5])
    else:
        mesh.textures = TexturesVertex(verts_features=[vertices_color])
    return mesh


def from_py3d_mesh(mesh):
    return mesh.verts_list()[0], mesh.faces_list()[0], mesh.textures.verts_features_packed()

def rotate_normalmap_by_angle(normal_map: np.ndarray, angle: float):
    """
    rotate along y-axis
    normal_map: np.array, shape=(H, W, 3) in [-1, 1]
    angle: float, in degree
    """
    angle = angle / 180 * np.pi
    R = np.array([[np.cos(angle), 0, np.sin(angle)], [0, 1, 0], [-np.sin(angle), 0, np.cos(angle)]])
    return np.dot(normal_map.reshape(-1, 3), R.T).reshape(normal_map.shape)

# from view coord to front view world coord
def rotate_normals(normal_pils, return_types='np', rotate_direction=1) -> np.ndarray:  # [0, 255]
    n_views = len(normal_pils)
    ret = []
    for idx, rgba_normal in enumerate(normal_pils):
        # rotate normal
        normal_np = np.array(rgba_normal)[:, :, :3] / 255           # in [-1, 1]
        alpha_np = np.array(rgba_normal)[:, :, 3] / 255             # in [0, 1]
        normal_np = normal_np * 2 - 1
        normal_np = rotate_normalmap_by_angle(normal_np, rotate_direction * idx * (360 / n_views))
        normal_np = (normal_np + 1) / 2
        normal_np = normal_np * alpha_np[..., None]                 # make bg black
        rgba_normal_np = np.concatenate([normal_np * 255, alpha_np[:, :, None] * 255] , axis=-1)
        if return_types == 'np':
            ret.append(rgba_normal_np)
        elif return_types == 'pil':
            ret.append(Image.fromarray(rgba_normal_np.astype(np.uint8)))
        else:
            raise ValueError(f"return_types should be 'np' or 'pil', but got {return_types}")
    return ret


def rotate_normalmap_by_angle_torch(normal_map, angle):
    """
    rotate along y-axis
    normal_map: torch.Tensor, shape=(H, W, 3) in [-1, 1], device='cuda'
    angle: float, in degree
    """
    angle = torch.tensor(angle / 180 * np.pi).to(normal_map)
    R = torch.tensor([[torch.cos(angle), 0, torch.sin(angle)], 
                      [0, 1, 0], 
                      [-torch.sin(angle), 0, torch.cos(angle)]]).to(normal_map)
    return torch.matmul(normal_map.view(-1, 3), R.T).view(normal_map.shape)

def do_rotate(rgba_normal, angle):
    rgba_normal = torch.from_numpy(rgba_normal).float().cuda() / 255
    rotated_normal_tensor = rotate_normalmap_by_angle_torch(rgba_normal[..., :3] * 2 - 1, angle)
    rotated_normal_tensor = (rotated_normal_tensor + 1) / 2
    rotated_normal_tensor = rotated_normal_tensor * rgba_normal[:, :, [3]]    # make bg black
    rgba_normal_np = torch.cat([rotated_normal_tensor * 255, rgba_normal[:, :, [3]] * 255], dim=-1).cpu().numpy()
    return rgba_normal_np

def rotate_normals_torch(normal_pils, return_types='np', rotate_direction=1):
    n_views = len(normal_pils)
    ret = []
    for idx, rgba_normal in enumerate(normal_pils):
        # rotate normal
        angle = rotate_direction * idx * (360 / n_views)
        rgba_normal_np = do_rotate(np.array(rgba_normal), angle)
        if return_types == 'np':
            ret.append(rgba_normal_np)
        elif return_types == 'pil':
            ret.append(Image.fromarray(rgba_normal_np.astype(np.uint8)))
        else:
            raise ValueError(f"return_types should be 'np' or 'pil', but got {return_types}")
    return ret

def get_normal_map_masks(imgs):
    """Only works for normal"""
    if not isinstance(imgs, list):
        imgs = [imgs]
        
    rets = []
    for img in imgs:
        arr = np.array(img)

        # remove color from normal map
        if arr.shape[-1] == 4:
            arr = arr[..., :3]
        # calc diffs
        base = arr[0, 0]
        diffs = np.abs(arr.astype(np.int32) - base.astype(np.int32)).sum(axis=-1)
        alpha = (diffs <= 80)
        
        arr[alpha] = 255
        alpha = ~alpha
        #arr = np.concatenate([arr, alpha[..., None].astype(np.int32) * 255], axis=-1)

        #rets.append(Image.fromarray(arr.astype(np.uint8)))
        rets.append(torch.from_numpy(alpha))
    return rets

def change_bkgd(img_pils, new_bkgd=(0., 0., 0.)):
    ret = []
    new_bkgd = np.array(new_bkgd).reshape(1, 1, 3)
    for rgba_img in img_pils:
        img_np = np.array(rgba_img)[:, :, :3] / 255
        alpha_np = np.array(rgba_img)[:, :, 3] / 255
        ori_bkgd = img_np[:1, :1]
        # color = ori_color * alpha + bkgd * (1-alpha)
        # ori_color = (color - bkgd * (1-alpha)) / alpha
        alpha_np_clamp = np.clip(alpha_np, 1e-6, 1) # avoid divide by zero
        ori_img_np = (img_np - ori_bkgd * (1 - alpha_np[..., None])) / alpha_np_clamp[..., None]
        img_np = np.where(alpha_np[..., None] > 0.05, ori_img_np * alpha_np[..., None] + new_bkgd * (1 - alpha_np[..., None]), new_bkgd)
        rgba_img_np = np.concatenate([img_np * 255, alpha_np[..., None] * 255], axis=-1)
        ret.append(Image.fromarray(rgba_img_np.astype(np.uint8)))
    return ret

def change_bkgd_to_normal(normal_pils) -> List[Image.Image]:
    n_views = len(normal_pils)
    ret = []
    for idx, rgba_normal in enumerate(normal_pils):
        # calcuate background normal
        target_bkgd = rotate_normalmap_by_angle(np.array([[[0., 0., 1.]]]), idx * (360 / n_views))
        normal_np = np.array(rgba_normal)[:, :, :3] / 255           # in [-1, 1]
        alpha_np = np.array(rgba_normal)[:, :, 3] / 255             # in [0, 1]
        normal_np = normal_np * 2 - 1
        old_bkgd = normal_np[:1,:1]
        normal_np[alpha_np > 0.05] = (normal_np[alpha_np > 0.05] - old_bkgd * (1 - alpha_np[alpha_np > 0.05][..., None])) / alpha_np[alpha_np > 0.05][..., None]
        normal_np = normal_np * alpha_np[..., None] + target_bkgd * (1 - alpha_np[..., None])
        normal_np = (normal_np + 1) / 2
        rgba_normal_np = np.concatenate([normal_np * 255, alpha_np[..., None] * 255] , axis=-1)
        ret.append(Image.fromarray(rgba_normal_np.astype(np.uint8)))
    return ret


def fix_vert_color_glb(mesh_path):
    from pygltflib import GLTF2, Material, PbrMetallicRoughness
    obj1 = GLTF2().load(mesh_path)
    obj1.meshes[0].primitives[0].material = 0
    obj1.materials.append(Material(
        pbrMetallicRoughness = PbrMetallicRoughness(
            baseColorFactor = [1.0, 1.0, 1.0, 1.0],
            metallicFactor = 0.,
            roughnessFactor = 1.0,
        ),
        emissiveFactor = [0.0, 0.0, 0.0],
        doubleSided = True,
    ))
    obj1.save(mesh_path)


def srgb_to_linear(c_srgb):
    c_linear = np.where(c_srgb <= 0.04045, c_srgb / 12.92, ((c_srgb + 0.055) / 1.055) ** 2.4)
    return c_linear.clip(0, 1.)


def save_py3dmesh_with_trimesh_fast(meshes: Meshes, save_glb_path, apply_sRGB_to_LinearRGB=True):
    # convert from pytorch3d meshes to trimesh mesh
    vertices = meshes.verts_packed().cpu().float().numpy()
    triangles = meshes.faces_packed().cpu().long().numpy()
    np_color = meshes.textures.verts_features_packed().cpu().float().numpy()
    if save_glb_path.endswith(".glb"):
        # rotate 180 along +Y
        vertices[:, [0, 2]] = -vertices[:, [0, 2]]

    if apply_sRGB_to_LinearRGB:
        np_color = srgb_to_linear(np_color)
    assert vertices.shape[0] == np_color.shape[0]
    assert np_color.shape[1] == 3
    assert 0 <= np_color.min() and np_color.max() <= 1, f"min={np_color.min()}, max={np_color.max()}"
    mesh = trimesh.Trimesh(vertices=vertices, faces=triangles, vertex_colors=np_color)
    mesh.remove_unreferenced_vertices()
    # save mesh
    mesh.export(save_glb_path)
    if save_glb_path.endswith(".glb"):
        fix_vert_color_glb(save_glb_path)
    print(f"saving to {save_glb_path}")


def save_glb_and_video(save_mesh_prefix: str, meshes: Meshes, with_timestamp=True, dist=3.5, azim_offset=180, resolution=512, fov_in_degrees=1 / 1.15, cam_type="ortho", view_padding=60, export_video=True) -> Tuple[str, str]:
    import time
    if '.' in save_mesh_prefix:
        save_mesh_prefix = ".".join(save_mesh_prefix.split('.')[:-1])
    if with_timestamp:
        save_mesh_prefix = save_mesh_prefix + f"_{int(time.time())}"
    ret_mesh = save_mesh_prefix + ".glb"
    # optimizied version
    save_py3dmesh_with_trimesh_fast(meshes, ret_mesh)
    return ret_mesh, None


def simple_clean_mesh(pyml_mesh: ml.Mesh, apply_smooth=True, stepsmoothnum=1, apply_sub_divide=False, sub_divide_threshold=0.25):
    ms = ml.MeshSet()
    ms.add_mesh(pyml_mesh, "cube_mesh")
    
    if apply_smooth:
        ms.apply_filter("apply_coord_laplacian_smoothing", stepsmoothnum=stepsmoothnum, cotangentweight=False)
    if apply_sub_divide:    # 5s, slow
        ms.apply_filter("meshing_repair_non_manifold_vertices")
        ms.apply_filter("meshing_repair_non_manifold_edges", method='Remove Faces')
        ms.apply_filter("meshing_surface_subdivision_loop", iterations=2, threshold=PercentageValue(sub_divide_threshold))
    return meshlab_mesh_to_py3dmesh(ms.current_mesh())

def advanced_clean_mesh(pyml_mesh: ml.Mesh, 
    apply_smooth=True, 
    stepsmoothnum=1, 
    apply_sub_divide=False, 
    sub_divide_threshold=0.25, 
    v_pct=1,
    min_f=0,
    min_d=0,
    repair=False,
    remesh=False,
    remesh_size=0.01,
    remesh_iters=3,
    close_holes=False,
    max_hole_size=1000,
    refine_holes=False):
    ms = ml.MeshSet()
    ms.add_mesh(pyml_mesh, "cube_mesh_adv")
    
    # filters
    ms.meshing_remove_unreferenced_vertices()  # verts not refed by any faces

    if v_pct > 0:
        ms.meshing_merge_close_vertices(
            threshold=ml.PercentageValue(v_pct)
        )  # 1/10000 of bounding box diagonal

    ms.meshing_remove_duplicate_faces()  # faces defined by the same verts
    ms.meshing_remove_null_faces()  # faces with area == 0

    if min_d > 0:
        ms.meshing_remove_connected_component_by_diameter(
            mincomponentdiag=ml.PercentageValue(min_d)
        )

    if min_f > 0:
        ms.meshing_remove_connected_component_by_face_number(mincomponentsize=min_f)

    # be careful: may lead to strangely missing triangles...
    if repair:
        # ms.meshing_remove_t_vertices(method=0, threshold=40, repeat=True)
        ms.meshing_repair_non_manifold_edges(method=0)
        ms.meshing_repair_non_manifold_vertices(vertdispratio=0)
    
    if close_holes:
        ms.meshing_close_holes(maxholesize = max_hole_size, refinehole = refine_holes)

    if remesh:
        # ms.apply_coord_taubin_smoothing()
        ms.meshing_isotropic_explicit_remeshing(
            iterations=remesh_iters, targetlen=ml.PureValue(remesh_size)
        )
    
    ms.current_mesh().compact()
    
    if apply_smooth:
        ms.apply_filter("apply_coord_laplacian_smoothing", stepsmoothnum=stepsmoothnum, cotangentweight=False)
    if apply_sub_divide:    # 5s, slow
        ms.apply_filter("meshing_repair_non_manifold_vertices")
        ms.apply_filter("meshing_repair_non_manifold_edges", method='Remove Faces')
        ms.apply_filter("meshing_surface_subdivision_loop", iterations=2, threshold=PercentageValue(sub_divide_threshold))
        
    return meshlab_mesh_to_py3dmesh(ms.current_mesh())

def close_mesh_holes(pyml_mesh: ml.Mesh, cleanup_first=True, max_hole_size=1000, refine_holes=False):
    ms = ml.MeshSet()
    ms.add_mesh(pyml_mesh, "holes_mesh")

    # filters
    if cleanup_first:
        ms.meshing_remove_unreferenced_vertices()  # verts not refed by any faces
        ms.meshing_remove_duplicate_faces()  # faces defined by the same verts
        ms.meshing_remove_null_faces()  # faces with area == 0

    ms.meshing_close_holes(maxholesize = max_hole_size, refinehole = refine_holes)

    # extract mesh
    m = ms.current_mesh()
    m.compact()

    return meshlab_mesh_to_py3dmesh(m)

def sharpen_mesh(pyml_mesh: ml.Mesh, cleanup_first=True, weight=0.4, iterations=25):
    ms = ml.MeshSet()
    ms.add_mesh(pyml_mesh, "sharpen_mesh")

    # filters
    if cleanup_first:
        ms.meshing_remove_unreferenced_vertices()  # verts not refed by any faces
        ms.meshing_remove_duplicate_faces()  # faces defined by the same verts
        ms.meshing_remove_null_faces()  # faces with area == 0
        
    ms.apply_coord_unsharp_mask(weight = weight, weightorig = 1.000000, iterations = iterations)

    # extract mesh
    m = ms.current_mesh()
    m.compact()

    return meshlab_mesh_to_py3dmesh(m)

def mesh_map_vert_color_to_texture(pyml_mesh: ml.Mesh, cleanup_first=True, region_num=25, overlap=False, texture_width=2048, texture_height=2048):
    ms = ml.MeshSet()
    ms.add_mesh(pyml_mesh, "texture_mesh")
    ms.add_mesh(pyml_mesh, "texture_mesh2")

    # filters
    if cleanup_first:
        ms.meshing_remove_unreferenced_vertices()  # verts not refed by any faces
        ms.meshing_remove_duplicate_faces()  # faces defined by the same verts
        ms.meshing_remove_null_faces()  # faces with area == 0
    
    ms.set_current_mesh(1)
    ms.generate_voronoi_atlas_parametrization(regionnum = region_num, overlapflag = overlap)
    ms.set_current_mesh(0)
    ms.transfer_attributes_to_texture_per_vertex(sourcemesh=0, targetmesh=2, attributeenum='Vertex Color', textw=texture_width, texth=texture_height)
    ms.set_current_mesh(0)
    ms.save_current_mesh("C:/test0.obj", save_textures = True)
    ms.set_current_mesh(1)
    ms.save_current_mesh("C:/test1.obj", save_textures = True)
    ms.set_current_mesh(2)
    ms.save_current_mesh("C:/test2.obj", save_textures = True)
    
    # extract mesh
    m = ms.current_mesh()
    m.compact()
    ms.save_current_mesh("C:/test.obj", save_textures = True)

    return meshlab_mesh_to_py3dmesh(m)

def mesh_remove_isolated_pieces(pyml_mesh: ml.Mesh, cleanup_first=True, diag_pcnt=0.1, remove_unref=True):
    ms = ml.MeshSet()
    ms.add_mesh(pyml_mesh, "iso_piece_mesh")

    # filters
    if cleanup_first:
        ms.meshing_remove_unreferenced_vertices()  # verts not refed by any faces
        ms.meshing_remove_duplicate_faces()  # faces defined by the same verts
        ms.meshing_remove_null_faces()  # faces with area == 0
        
    if diag_pcnt > 1.0:
        diag_pcnt = 1.0
    elif diag_pcnt < 0.0:
        diag_pcnt = 0.0
    
    ms.meshing_remove_connected_component_by_diameter(mincomponentdiag = ml.PercentageValue((diag_pcnt * 100.0)), removeunref = remove_unref)
    
    ms.meshing_remove_unreferenced_vertices()  # verts not refed by any faces
    ms.meshing_remove_duplicate_faces()  # faces defined by the same verts
    ms.meshing_remove_null_faces()  # faces with area == 0

    # extract mesh
    m = ms.current_mesh()
    m.compact()

    return meshlab_mesh_to_py3dmesh(m)


def expand2square(pil_img, background_color):
    width, height = pil_img.size
    if width == height:
        return pil_img
    elif width > height:
        result = Image.new(pil_img.mode, (width, width), background_color)
        result.paste(pil_img, (0, (width - height) // 2))
        return result
    else:
        result = Image.new(pil_img.mode, (height, height), background_color)
        result.paste(pil_img, ((height - width) // 2, 0))
        return result


def simple_image_preprocess(input_image, rembg_session=session, background_color=255):
    RES = 2048
    input_image.thumbnail([RES, RES], Image.Resampling.LANCZOS)
    if input_image.mode != 'RGBA':
        image_rem = input_image.convert('RGBA')
        input_image = remove(image_rem, alpha_matting=False, session=rembg_session)

    arr = np.asarray(input_image)
    alpha = np.asarray(input_image)[:, :, -1]
    x_nonzero = np.nonzero((alpha > 60).sum(axis=1))
    y_nonzero = np.nonzero((alpha > 60).sum(axis=0))
    x_min = int(x_nonzero[0].min())
    y_min = int(y_nonzero[0].min())
    x_max = int(x_nonzero[0].max())
    y_max = int(y_nonzero[0].max())
    arr = arr[x_min: x_max, y_min: y_max]
    input_image = Image.fromarray(arr)
    input_image = expand2square(input_image, (background_color, background_color, background_color, 0))
    return input_image

def init_target(img_pils, new_bkgd=(0., 0., 0.), device="cuda"):
    # Convert the background color to a PyTorch tensor
    new_bkgd = torch.tensor(new_bkgd, dtype=torch.float32).view(1, 1, 3).to(device)
    
    # Convert all images to PyTorch tensors and process them
    imgs = torch.stack([torch.from_numpy(np.array(img, dtype=np.float32)) for img in img_pils]).to(device) / 255
    img_nps = imgs[..., :3]
    alpha_nps = imgs[..., 3]
    ori_bkgds = img_nps[:, :1, :1]
    
    # Avoid divide by zero and calculate the original image
    alpha_nps_clamp = torch.clamp(alpha_nps, 1e-6, 1)
    ori_img_nps = (img_nps - ori_bkgds * (1 - alpha_nps.unsqueeze(-1))) / alpha_nps_clamp.unsqueeze(-1)
    ori_img_nps = torch.clamp(ori_img_nps, 0, 1)
    img_nps = torch.where(alpha_nps.unsqueeze(-1) > 0.05, ori_img_nps * alpha_nps.unsqueeze(-1) + new_bkgd * (1 - alpha_nps.unsqueeze(-1)), new_bkgd)

    rgba_img_np = torch.cat([img_nps, alpha_nps.unsqueeze(-1)], dim=-1)
    return rgba_img_np