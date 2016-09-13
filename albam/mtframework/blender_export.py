from collections import OrderedDict
import ctypes
from io import BytesIO
import posixpath
import ntpath
import os
import tempfile
import re
import struct
try:
    import bpy
except ImportError:
    pass


from albam.exceptions import ExportError
from albam.mtframework.mod import (
    Mesh156,
    MaterialData,
    BonePalette,
    CLASSES_TO_VERTEX_FORMATS,
    VERTEX_FORMATS_TO_CLASSES,
    )
from albam.mtframework import Arc, Mod156, Tex112
from albam.mtframework.utils import vertices_export_locations
from albam.utils import (
    pack_half_float,
    get_offset,
    triangles_list_to_triangles_strip,
    z_up_to_y_up,
    get_bounding_box_positions_from_blender_objects,
    get_bone_count_from_blender_objects,
    get_textures_from_blender_objects,
    get_materials_from_blender_objects,
    get_mesh_count_from_blender_objects,
    get_vertex_count_from_blender_objects,
    get_bone_indices_and_weights_per_vertex,
    get_uvs_per_vertex,
    ensure_ntpath,
    )
from albam.registry import blender_registry


# Taken from: RE5->uOm0000Damage.arc->/pawn/om/om0000/model/om0000.mod
# Not entirely sure what it represents, but it works to use in all models so far
CUBE_BBOX = [0.0, 0.0, 0.0, 0.0,
             0.0, 50.0, 0.0, 86.6025390625,
             -50.0, 0.0, -50.0, 0.0,
             50.0, 100.0, 50.0, 0.0,
             1.0, 0.0, 0.0, 0.0,
             0.0, 1.0, 0.0, 0.0,
             0.0, 0.0, 1.0, 0.0,
             0.0, 50.0, 0.0, 1.0,
             50.0, 50.0, 50.0, 0.0]

# Taken from RE5->uPlChrisNormal.arc->pawn/pl/pl00/model/pl0000.mod->materials_data_array[16]
DEFAULT_MATERIAL_FLOATS = (0.0, 1.0, 0.04, 0.0,
                           1.0, 0.3, 1.0, 1.0,
                           1.0, 0.0, 0.25, 36.0,
                           0.0, 0.5, 0.0, 0.0,
                           0.0, 0.0, 0.0, 0.0,
                           1.0, 0.2, 0.0, 0.0,
                           0.0, 0.0)


@blender_registry.register_function('export', b'ARC\x00')
def export_arc(blender_object, file_path):
    '''Exports an arc file containing mod and tex files, among others from a
    previously imported arc.'''
    mods = {}
    try:
        saved_arc = Arc(file_path=BytesIO(blender_object.albam_imported_item.data))
    except AttributeError:
        raise ExportError('Object {0} did not come from the original arc'.format(blender_object.name))

    for child in blender_object.children:
        try:
            basename = posixpath.basename(child.name)
            folder = child.albam_imported_item.folder
            if os.sep == ntpath.sep:  # Windows
                mod_filepath = ntpath.join(ensure_ntpath(folder), basename)
            else:
                mod_filepath = os.path.join(folder, basename)
        except AttributeError:
            raise ExportError('Object {0} did not come from the original arc'.format(child.name))
        assert child.albam_imported_item.file_type == 'mtframework.mod'
        mod, textures = export_mod156(child)
        mods[mod_filepath] = (mod, textures)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_slash_ending = tmpdir + os.sep if not tmpdir.endswith(os.sep) else tmpdir
        saved_arc.unpack(tmpdir)
        mod_files = [os.path.join(root, f) for root, _, files in os.walk(tmpdir)
                     for f in files if f.endswith('.mod')]
        new_tex_files = set()
        for modf in mod_files:
            rel_path = modf.split(tmpdir_slash_ending)[1]
            try:
                new_mod, mod_textures = mods[rel_path]
            except KeyError:
                raise ExportError("Can't export to arc, a mod file is missing: {}".format(rel_path))

            with open(modf, 'wb') as w:
                w.write(new_mod)

            for texture in mod_textures:
                tex = Tex112.from_dds(file_path=bpy.path.abspath(texture.image.filepath))
                try:
                    tex.unk_float_1 = texture.albam_imported_texture_value_1
                    tex.unk_float_2 = texture.albam_imported_texture_value_2
                    tex.unk_float_3 = texture.albam_imported_texture_value_3
                    tex.unk_float_4 = texture.albam_imported_texture_value_4
                except AttributeError:
                    pass

                tex_name = os.path.basename(texture.image.filepath)
                tex_filepath = os.path.join(os.path.dirname(modf), tex_name.replace('.dds', '.tex'))
                new_tex_files.add(tex_filepath)
                with open(tex_filepath, 'wb') as w:
                    w.write(tex)
        # probably other files can reference textures besides mod, this is in case
        # textures applied have other names.
        # TODO: delete only textures referenced from saved_mods at import time
        # unused_tex_files = tex_files - new_tex_files
        # for utex in unused_tex_files:
        #    os.unlink(utex)
        new_arc = Arc.from_dir(tmpdir)
        with open(file_path, 'wb') as w:
            w.write(new_arc)


def export_mod156(parent_blender_object):
    '''The blender_object provided should have meshes as children'''
    try:
        saved_mod = Mod156(file_path=BytesIO(parent_blender_object.albam_imported_item.data))
    except AttributeError:
        raise ExportError("Can't export '{0}' to Mod156, the model to be exported "
                          "wasn't imported using Albam"
                          .format(parent_blender_object.name))

    children_objects = [child for child in parent_blender_object.children]
    meshes_children = [c for c in children_objects if c.type == 'MESH']
    # TODO: decide what to do with EMPTY objects, which are failed imports
    bounding_box = get_bounding_box_positions_from_blender_objects(children_objects)

    mesh_count = len(meshes_children)
    header = struct.unpack('f', struct.pack('4B', mesh_count, 0, 0, 0))[0]
    meshes_array_2 = ctypes.c_float * ((mesh_count * 36) + 1)
    floats = [header] + CUBE_BBOX * mesh_count
    meshes_array_2 = meshes_array_2(*floats)

    textures_array, materials_array, materials_mapping = _export_textures_and_materials(children_objects)
    bone_palettes = _create_bone_palettes(meshes_children)
    meshes_array, vertex_buffer, index_buffer = _export_meshes(children_objects,
                                                               bounding_box,
                                                               bone_palettes,
                                                               materials_mapping)
    bone_palette_array = (BonePalette * len(bone_palettes))()
    for i, bp in enumerate(bone_palettes.values()):
        bone_palette_array[i].unk_01 = len(bp)
        if len(bp) != 32:
            padding = 32 - len(bp)
            bp = bp + [0] * padding
        bone_palette_array[i].values = (ctypes.c_ubyte * len(bp))(*bp)

    bone_count = get_bone_count_from_blender_objects([parent_blender_object])
    if not bone_count:
        bones_array_offset = 0
    elif bone_count and saved_mod.unk_08:
        bones_array_offset = 176 + len(saved_mod.unk_12)
    else:
        bones_array_offset = 176

    mod = Mod156(id_magic=b'MOD',
                 version=156,
                 version_rev=1,
                 bone_count=bone_count,
                 mesh_count=get_mesh_count_from_blender_objects(children_objects),
                 material_count=len(materials_array),
                 vertex_count=get_vertex_count_from_blender_objects(children_objects),
                 face_count=(ctypes.sizeof(index_buffer) // 2) + 1,
                 edge_count=0,  # TODO: add edge_count
                 vertex_buffer_size=ctypes.sizeof(vertex_buffer),
                 vertex_buffer_2_size=len(saved_mod.vertex_buffer_2),
                 texture_count=len(textures_array),
                 group_count=saved_mod.group_count,
                 bones_array_offset=bones_array_offset,
                 group_data_array=saved_mod.group_data_array,
                 bone_palette_count=len(bone_palette_array),
                 sphere_x=saved_mod.sphere_x,
                 sphere_y=saved_mod.sphere_y,
                 sphere_z=saved_mod.sphere_z,
                 sphere_w=saved_mod.sphere_w,
                 box_min_x=saved_mod.box_min_x,
                 box_min_y=saved_mod.box_min_y,
                 box_min_z=saved_mod.box_min_z,
                 box_min_w=saved_mod.box_min_w,
                 box_max_x=saved_mod.box_max_x,
                 box_max_y=saved_mod.box_max_y,
                 box_max_z=saved_mod.box_max_z,
                 box_max_w=saved_mod.box_max_w,
                 unk_01=saved_mod.unk_01,
                 unk_02=saved_mod.unk_02,
                 unk_03=saved_mod.unk_03,
                 unk_04=saved_mod.unk_04,
                 unk_05=saved_mod.unk_05,
                 unk_06=saved_mod.unk_06,
                 unk_07=saved_mod.unk_07,
                 unk_08=saved_mod.unk_08,
                 unk_09=saved_mod.unk_09,
                 unk_10=saved_mod.unk_10,
                 unk_11=saved_mod.unk_11,
                 unk_12=saved_mod.unk_12,
                 bones_array=saved_mod.bones_array,
                 bones_unk_matrix_array=saved_mod.bones_unk_matrix_array,
                 bones_world_transform_matrix_array=saved_mod.bones_world_transform_matrix_array,
                 unk_13=saved_mod.unk_13,
                 bone_palette_array=bone_palette_array,
                 textures_array=textures_array,
                 materials_data_array=materials_array,
                 meshes_array=meshes_array,
                 meshes_array_2=meshes_array_2,
                 vertex_buffer=vertex_buffer,
                 vertex_buffer_2=saved_mod.vertex_buffer_2,
                 index_buffer=index_buffer
                 )
    mod.bones_array_offset = get_offset(mod, 'bones_array') if mod.bone_count else 0
    mod.group_offset = get_offset(mod, 'group_data_array')
    mod.textures_array_offset = get_offset(mod, 'textures_array')
    mod.meshes_array_offset = get_offset(mod, 'meshes_array')
    mod.vertex_buffer_offset = get_offset(mod, 'vertex_buffer')
    mod.vertex_buffer_2_offset = get_offset(mod, 'vertex_buffer_2')
    mod.index_buffer_offset = get_offset(mod, 'index_buffer')
    return mod, get_textures_from_blender_objects(children_objects)


def _export_vertices(blender_mesh_object, bounding_box, mesh_index, bone_palette):
    blender_mesh = blender_mesh_object.data
    vertex_count = len(blender_mesh.vertices)
    weights_per_vertex = get_bone_indices_and_weights_per_vertex(blender_mesh_object)
    # TODO: check the number of uv layers
    uvs_per_vertex = get_uvs_per_vertex(blender_mesh_object.data, blender_mesh_object.data.uv_layers[0])
    max_bones_per_vertex = max({len(data) for data in weights_per_vertex.values()}, default=0)
    if max_bones_per_vertex > 8:
        raise RuntimeError("The mesh '{}' contains some vertex that are weighted by "
                           "more than 8 bones, which is not supported. Fix it and try again"
                           .format(blender_mesh.name))
    VF = VERTEX_FORMATS_TO_CLASSES[max_bones_per_vertex]

    for vertex_index, (uv_x, uv_y) in uvs_per_vertex.items():
        # flipping for dds textures
        uv_y *= -1
        uv_x = pack_half_float(uv_x)
        uv_y = pack_half_float(uv_y)
        uvs_per_vertex[vertex_index] = (uv_x, uv_y)

    if uvs_per_vertex and len(uvs_per_vertex) != vertex_count:
        # TODO: logging
        print('There are some vertices with no uvs in mesh in {}.'
              'Vertex count: {} UVs per vertex: {}'.format(blender_mesh.name, vertex_count,
                                                           len(uvs_per_vertex)))

    box_width = abs(bounding_box.min_x * 100) + abs(bounding_box.max_x * 100)
    box_height = abs(bounding_box.min_y * 100) + abs(bounding_box.max_y * 100)
    box_length = abs(bounding_box.min_z * 100) + abs(bounding_box.max_z * 100)

    vertices_array = (VF * vertex_count)()
    has_bones = hasattr(VF, 'bone_indices')
    has_second_uv_layer = hasattr(VF, 'uv2_x')
    has_tangents = hasattr(VF, 'tangent_x')
    for vertex_index, vertex in enumerate(blender_mesh.vertices):
        vertex_struct = vertices_array[vertex_index]
        if weights_per_vertex:
            weights_data = weights_per_vertex[vertex_index]   # list of (bone_index, value)
            bone_indices = [bone_palette.index(bone_index) for bone_index, _ in weights_data]
            weight_values = [round(weight_value * 255) for _, weight_value in weights_data]
            total_weight = sum(weight_values)
            # each vertex has to be influenced 100%. Padding if it's not.
            if total_weight < 255:
                to_fill = 255 - total_weight
                percentages = [(w / total_weight) * 100 for w in weight_values]
                weight_values = [round(w + ((percentages[i] * to_fill) / 100)) for i, w in enumerate(weight_values)]
                # XXX tmp for 8 bone_indices other hack
                excess = 255 - sum(weight_values)
                if excess:
                    weight_values[0] -= 1
                # XXX more quick Saturday hack
                if sum(weight_values) < 255:
                    missing = 255 - sum(weight_values)
                    weight_values[0] += missing

        else:
            bone_indices = []
            weight_values = []

        xyz = (vertex.co[0] * 100, vertex.co[1] * 100, vertex.co[2] * 100)
        xyz = z_up_to_y_up(xyz)
        if has_bones:
            # applying bounding box constraints
            xyz = vertices_export_locations(xyz, box_width, box_length, box_height)
        vertex_struct.position_x = xyz[0]
        vertex_struct.position_y = xyz[1]
        vertex_struct.position_z = xyz[2]
        vertex_struct.position_w = 32767
        # guessing for now:
        # using Counter([v.normal_<x,y,z,w> for i, mesh in enumerate(mod.meshes_array)
        #               for v in get_vertices_array(original, original.meshes_array[i])]).most_common(10)
        vertex_struct.normal_x = 127
        vertex_struct.normal_y = 127
        vertex_struct.normal_z = 0
        vertex_struct.normal_w = -1
        if has_tangents:
            vertex_struct.tangent_x = 53
            vertex_struct.tangent_y = 53
            vertex_struct.tangent_z = 53
            vertex_struct.tangent_w = -1

        if has_bones:
            array_size = ctypes.sizeof(vertex_struct.bone_indices)
            try:
                vertex_struct.bone_indices = (ctypes.c_ubyte * array_size)(*bone_indices)
                vertex_struct.weight_values = (ctypes.c_ubyte * array_size)(*weight_values)
            except IndexError:
                # TODO: proper logging
                print('bone_indices', bone_indices, 'array_size', array_size)
                print('VF', VF)
                raise
        try:
            vertex_struct.uv_x = uvs_per_vertex.get(vertex_index, (0, 0))[0] if uvs_per_vertex else 0
            vertex_struct.uv_y = uvs_per_vertex.get(vertex_index, (0, 0))[1] if uvs_per_vertex else 0
        except:
            pass
        if has_second_uv_layer:
            vertex_struct.uv2_x = 0
            vertex_struct.uv2_y = 0
    return vertices_array


def _create_bone_palettes(blender_mesh_objects):
    bone_palette_dicts = []
    MAX_BONE_PALETTE_SIZE = 32

    bone_palette = {'mesh_indices': set(), 'bone_indices': set()}
    for i, mesh in enumerate(blender_mesh_objects):
        # XXX case where bone names are not integers
        bone_indices = {int(vg.name) for vg in mesh.vertex_groups}
        assert len(bone_indices) <= MAX_BONE_PALETTE_SIZE, "Mesh {} is influenced by more than 32 bones, which is not supported".format(i)
        current = bone_palette['bone_indices']
        potential = current.union(bone_indices)
        if len(potential) > MAX_BONE_PALETTE_SIZE:
            bone_palette_dicts.append(bone_palette)
            bone_palette = {'mesh_indices': {i}, 'bone_indices': set(bone_indices)}
        else:
            bone_palette['mesh_indices'].add(i)
            bone_palette['bone_indices'].update(bone_indices)

    bone_palette_dicts.append(bone_palette)

    final = OrderedDict([(frozenset(bp['mesh_indices']), sorted(bp['bone_indices']))
                        for bp in bone_palette_dicts])

    return final


def _infer_level_of_detail(name):
    LEVEL_OF_DETAIL_RE = re.compile(r'.*LOD_(?P<level_of_detail>\d+)$')
    match = LEVEL_OF_DETAIL_RE.match(name)
    if match:
        return int(match.group('level_of_detail'))
    return 1


def _export_meshes(blender_meshes, bounding_box, bone_palettes, materials_mapping):
    """
    No weird optimization or sharing of offsets in the vertex buffer.
    All the same offsets, different positions like pl0200.mod from
    uPl01ShebaCos1.arc
    No time to investigate why and how those are decided. I suspect it might have to
    do with location of the meshes
    """
    meshes_156 = (Mesh156 * len(blender_meshes))()
    vertex_buffer = bytearray()
    index_buffer = bytearray()

    vertex_position = 0
    face_position = 0
    for mesh_index, blender_mesh_ob in enumerate(blender_meshes):

        level_of_detail = _infer_level_of_detail(blender_mesh_ob.name)
        bone_palette_index = None
        bone_palette = []
        for bpi, (meshes_indices, bp) in enumerate(bone_palettes.items()):
            if mesh_index in meshes_indices:
                bone_palette_index = bpi
                bone_palette = bp
                break

        blender_mesh = blender_mesh_ob.data
        vertices_array = _export_vertices(blender_mesh_ob, bounding_box, mesh_index, bone_palette)
        vertex_buffer.extend(vertices_array)

        # TODO: is all this format conversion necessary?
        triangle_strips_python = triangles_list_to_triangles_strip(blender_mesh)
        # mod156 use global indices for verts, in case one only mesh is needed, probably
        triangle_strips_python = [e + vertex_position for e in triangle_strips_python]
        triangle_strips_ctypes = (ctypes.c_ushort * len(triangle_strips_python))(*triangle_strips_python)
        index_buffer.extend(triangle_strips_ctypes)

        vertex_count = len(blender_mesh.vertices)
        index_count = len(triangle_strips_python)

        m156 = meshes_156[mesh_index]
        try:
            m156.material_index = materials_mapping[blender_mesh.materials[0].name]
        except IndexError:
            # TODO: insert an empty generic material in this case
            raise ExportError('Mesh {} has no materials'.format(blender_mesh.name))
        m156.constant = 1
        m156.level_of_detail = level_of_detail
        m156.vertex_format = CLASSES_TO_VERTEX_FORMATS[type(vertices_array[0])]
        m156.vertex_stride = 32
        m156.vertex_count = vertex_count
        m156.vertex_index_end = vertex_position + vertex_count - 1
        m156.vertex_index_start_1 = vertex_position
        m156.vertex_offset = 0
        m156.face_position = face_position
        m156.face_count = index_count
        m156.face_offset = 0
        m156.vertex_index_start_2 = vertex_position
        m156.vertex_group_count = 1  # using 'TEST' bounding box
        m156.bone_palette_index = bone_palette_index

        # Needs research
        m156.group_index = 0
        m156.unk_01 = 0
        m156.unk_02 = 0
        m156.unk_03 = 0
        m156.unk_04 = 0
        m156.unk_05 = 0
        m156.unk_06 = 0
        m156.unk_07 = 0
        m156.unk_08 = 0
        m156.unk_09 = 0
        m156.unk_10 = 0
        m156.unk_11 = 0

        vertex_position += vertex_count
        face_position += index_count
    vertex_buffer = (ctypes.c_ubyte * len(vertex_buffer)).from_buffer(vertex_buffer)
    index_buffer = (ctypes.c_ushort * (len(index_buffer) // 2)).from_buffer(index_buffer)

    return meshes_156, vertex_buffer, index_buffer


def _export_textures_and_materials(blender_objects):
    textures = get_textures_from_blender_objects(blender_objects)
    blender_materials = get_materials_from_blender_objects(blender_objects)
    textures_array = ((ctypes.c_char * 64) * len(textures))()
    materials_data_array = (MaterialData * len(blender_materials))()
    materials_mapping = {}  # blender_material.name: material_id

    for i, texture in enumerate(textures):
        file_name = os.path.basename(texture.image.filepath)
        try:
            file_path = ntpath.join(texture.albam_imported_texture_folder, file_name)
        except AttributeError:
            raise ExportError('Texture {0} was not imported from an Arc file'.format(texture.name))
        try:
            file_path, _ = ntpath.splitext(file_path)
            textures_array[i] = (ctypes.c_char * 64)(*file_path.encode('ascii'))
        except UnicodeEncodeError:
            raise ExportError('Texture path {} is not in ascii'.format(file_path))
        if len(file_path) > 64:
            # TODO: what if relative path are used?
            raise ExportError('File path to texture {} is longer than 64 characters'
                              .format(file_path))

    for i, mat in enumerate(blender_materials):
        material_data = MaterialData()
        # TODO: unhardcode values using blender properties instead
        material_data.unk_01 = 2168619075
        material_data.unk_02 = 18563
        material_data.unk_03 = 2267538950
        material_data.unk_04 = 451
        material_data.unk_05 = 179374192
        material_data.unk_06 = 0
        material_data.unk_07 = (ctypes.c_float * 26)(*DEFAULT_MATERIAL_FLOATS)
        for texture_slot in mat.texture_slots:
            if not texture_slot:
                continue
            texture = texture_slot.texture
            if not texture:
                # ?
                continue
            # texture_indices expects index-1 based
            try:
                texture_index = textures.index(texture) + 1
            except ValueError:
                # TODO: logging
                raise RuntimeError('error in textures')
            material_data.texture_indices[texture.albam_imported_texture_type] = texture_index
        materials_data_array[i] = material_data
        materials_mapping[mat.name] = i

    return textures_array, materials_data_array, materials_mapping
