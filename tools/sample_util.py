import numpy as np
import open3d as o3d
import pathlib
import re
import random
import cv2
import pickle
from lib.datasets.kitti_utils import Calibration
from tools.dataset_util import Dataset
from sklearn.decomposition import PCA
from tools.box_util import boxes_bev_iou_cpu, rect2lidar, check_points_in_boxes3d
from lib.datasets.kitti_utils import Object3d


def merge_labels(labels, samples, calib_, image_shape):
    canvas = np.zeros(image_shape[:2], dtype=np.int8) - 1
    labels += [sample.to_label() for sample in samples]
    labels = sorted(labels, key=lambda x: x.pos[2], reverse=True)
    for i, label in enumerate(labels):
        corners = label.generate_corners3d()
        uv, _ = calib_.rect_to_img(corners)
        u_min = round(max(0, np.min(uv[:, 0])))
        v_min = round(max(0, np.min(uv[:, 1])))
        u_max = round(min(np.max(uv[:, 0]), image_shape[1]))
        v_max = round(min(np.max(uv[:, 1]), image_shape[0]))

        canvas[v_min: v_max, u_min: u_max] = i
        label.area = (v_max - v_min) * (u_max - u_min) + 1e-6
    for i, label in enumerate(labels):
        area = np.sum(canvas == i)
        label.area = 1 - area / label.area
        label.occlusion = area2occlusion(label.area)
        label.level = label.get_obj_level()
    return labels


def area2occlusion(area):
    if area < 0.1:
        return 0
    elif area < 0.4:
        return 1
    elif area < 0.8:
        return 2
    else:
        return 3


def to3d(image, depth, calib, bbox2d=None):
    assert image.shape[:2] == depth.shape
    h, w = depth.shape
    u = np.repeat(np.arange(w), h)
    v = np.tile(np.arange(h), w)
    d = depth[v, u]
    rgb = image[v, u][:, ::-1]
    if bbox2d:  # 对样本，需要附加其在图像的位置
        u += bbox2d[0]
        v += bbox2d[1]
    cord = calib.img_to_rect(u, v, d)
    return cord, rgb


def to2d(cord, rgb, calib):
    uv, d = calib.rect_to_img(cord[:, 0:3])
    u, v = np.round(uv[:, 0]).astype(int), np.round(uv[:, 1]).astype(int)
    # 图像大小可能与原图不一致
    width, height = u.max() + 1, v.max() + 1

    image = np.zeros((height, width, 3), dtype=np.uint8)
    depth = np.zeros((height, width), dtype=np.float32)
    image[v, u] = rgb
    depth[v, u] = d
    return image, depth


class SampleDatabase:
    def __init__(self,
                 database_path,
                 idx_list=None,
                 sample_num=30,
                 x_range=(-15., 15.),
                 z_range=(25., 65.),
                 random_flip=0.5):
        self.database_path = pathlib.Path(database_path)
        assert self.database_path.exists()
        self.image_path = self.database_path / "image"
        self.depth_path = self.database_path / "depth"
        # self.mask_path = self.database_path / "mask"
        with open(self.database_path / "kitti_car_database.pkl", "rb") as f:
            database = pickle.load(f)
        with open(self.database_path / "sample_image_database.pkl", "rb") as f:
            self.sample_image_database = pickle.load(f)
        with open(self.database_path / "sample_depth_database.pkl", "rb") as f:
            self.sample_depth_database = pickle.load(f)

        if idx_list is not None:
            [database.pop(key) for key in list(database.keys()) if key.split("_")[0] not in idx_list]
        self.database = list(database.values())
        self.sample_num = sample_num
        self.x_range = x_range
        self.z_range = z_range
        self.random_flip = random_flip

        self.pointer = len(database)
        self.indices = None

    @staticmethod
    def get_ry_(alpha, xyz_, calib_):
        uv_, _ = calib_.rect_to_img(xyz_.reshape(1, -1))
        ry_ = calib_.alpha2ry(alpha, uv_[:, 0])
        return ry_

    @staticmethod
    def get_y_on_plane(x, z, plane):
        a, b, c, d = plane
        y = - a * x - c * z - d
        y /= b
        return y

    @staticmethod
    def flip_sample(sample):
        sample = sample.copy()
        calib = sample['calib']
        h, w = sample['image_shape']
        calib.flip([w, h])
        u_min, _, u_max, _ = sample['bbox2d']
        sample['bbox2d'][0], sample['bbox2d'][2] = w - u_max, w - u_min
        ry = np.pi - sample['label'].ry
        if ry > np.pi: ry -= 2 * np.pi
        if ry < -np.pi: ry += 2 * np.pi
        sample['label'].ry = ry
        sample['label'].pos[0] *= -1
        sample['label'].alpha = calib.ry2alpha(ry, w - (u_max + u_min) / 2)
        sample['plane'][0] *= -1
        sample['flipped'] = True
        return sample

    def samples_from_database(self, num):
        pointer, indices, database = self.pointer, self.indices, self.database
        if pointer >= len(database):
            indices = np.random.permutation(len(database))
            pointer = 0
        samples = [database[idx] for idx in indices[pointer: pointer + num]]
        if len(samples) < num:
            samples += [database[idx] for idx in indices[: num - len(samples)]]

        pointer += len(samples)
        self.pointer = pointer
        self.indices = indices
        return samples

    def xyz_to_bbox3d(self, samples, xyz_, calib_, random_flip=0.):
        sample_num = len(samples)
        if sample_num == 0:
            return [], np.zeros((0, 7))

        for i, sample in enumerate(samples):
            if np.random.rand() < random_flip:  # 随机翻转
                samples[i] = self.flip_sample(sample)

        alpha = np.array([[s['label'].alpha] for s in samples])
        lhw = np.array([[s['label'].l, s['label'].h, s['label'].w] for s in samples])

        # 采样 bbox3d
        ry_ = np.array([self.get_ry_(alpha[i], xyz_[i], calib_) for i in range(sample_num)])
        bbox3d_ = np.concatenate([xyz_, lhw, ry_], axis=1)

        return samples, bbox3d_

    def sample_xyz(self, plane_=None, samples=None, xyz_=None):
        if samples is not None and xyz_ is not None:
            assert len(samples) == xyz_.shape[0]
        sample_num = int(self.sample_num) if samples is None else len(samples)
        sample_num = sample_num if xyz_ is None else xyz_.shape[0]
        if samples is None:
            samples = self.samples_from_database(sample_num)
        if xyz_ is None:
            assert plane_ is not None
            low_z, high_z = self.z_range
            low_x, high_x = self.x_range
            x_ = np.random.uniform(low=low_x, high=high_x, size=(sample_num, 1))
            z_ = np.random.uniform(low=low_z, high=high_z, size=(sample_num, 1))
            y_ = np.array([self.get_y_on_plane(x_[i], z_[i], plane_) for i in range(sample_num)])
            xyz_ = np.concatenate([x_, y_, z_], axis=1)
        return samples, xyz_

    @staticmethod
    def get_scene_type(pos):
        z = pos[:, 1]
        segments = {
            "a": (70 > z) & (z >= 45),  # 3049
            "b": (45 > z) & (z >= 30),  # 1846
            "c": (30 > z) & (z >= 15),  # 2057
            "d": (15 > z) & (z >= 0)  # 529
        }
        grid_sums = {key: np.sum(value) for key, value in segments.items()}
        scene_type = max(grid_sums, key=grid_sums.get)
        return scene_type

    def get_valid_grid(self, grid):
        pos2d = np.array(list(grid.keys()))
        dis = np.linalg.norm(pos2d, axis=1)

        scene_type = self.get_scene_type(pos2d)

        # 删除超过范围的 grid
        valid = dis < min(np.max(dis) - 10, 65)  # 最大距离附近的点不可信，超过 65m 的点不可信
        pos2d = pos2d[valid]

        # 选取指定范围的 grid
        state = {
            'a': lambda x: (60 > x[:, 1]) & (x[:, 1] >= 40),
            'b': lambda x: (50 > x[:, 1]) & (x[:, 1] >= 30),
            'c': lambda x: (40 > x[:, 1]) & (x[:, 1] >= 20),
            'd': lambda x: np.zeros_like(x[:, 1], dtype=bool)
        }
        valid = state[scene_type](pos2d)
        pos2d = pos2d[valid]

        return pos2d, scene_type

    def sample_from_grid(self, grid, grid_size=1.):
        pos2d, scene_type = self.get_valid_grid(grid)
        grid_sum = pos2d.shape[0]

        sample_num = grid_sum // 10
        samples = self.samples_from_database(sample_num)

        indices = np.random.choice(pos2d.shape[0], sample_num, replace=False)
        offset = np.random.uniform(-grid_size / 2, grid_size / 2, size=(sample_num, 2))
        pos2d = pos2d[indices]

        plane_ = [grid[(pos2d[i][0], pos2d[i][1])]["plane"] for i in range(sample_num)]
        x_, z_ = (pos2d + offset).T
        y_ = np.array([self.get_y_on_plane(x_[i], z_[i], plane_[i]) for i in range(sample_num)])

        xyz_ = np.vstack((x_, y_, z_)).T
        return samples, xyz_, scene_type

    def fixed_sample(self, xyz_, calib_, index):
        n = xyz_.shape[0]
        samples = [self.database[index] for i in range(n)]
        alpha = np.array([[s['label'].alpha] for s in samples])
        lhw = np.array([[s['label'].l, s['label'].h, s['label'].w] for s in samples])

        ry_ = np.array([self.get_ry_(alpha[i], xyz_[i], calib_) for i in range(n)])
        bbox3d_ = np.concatenate([xyz_, lhw, ry_], axis=1)
        return samples, bbox3d_

    @staticmethod
    def check_normal_angle(normal, max_degree):
        assert normal.shape[0] == 3
        limit = np.cos(np.radians(max_degree))
        norm = np.linalg.norm(normal)
        cos = np.abs(normal[1]) / norm  # abs: 法向量不一定向下
        return cos >= limit

    @staticmethod
    def sample_put_on_plane(bbox3d, ground, radius=3, min_num=25, max_var=0.5e-2, max_degree=20):
        bbox3d = bbox3d.copy()
        flag = np.zeros((bbox3d.shape[0]), dtype=bool)
        for i, pos in enumerate(bbox3d[:, :3]):
            distance = np.linalg.norm(ground - pos, axis=1)
            nearby = ground[distance < radius]
            if nearby.shape[0] < min_num:
                continue

            pca = PCA(n_components=3)
            pca.fit(nearby)
            normal = pca.components_[2]
            var = pca.explained_variance_ratio_[2]
            if var > max_var:
                continue
            if not SampleDatabase.check_normal_angle(normal, max_degree):
                continue
            d = -normal.dot(np.mean(nearby, axis=0))
            bbox3d[i, 1] = SampleDatabase.get_y_on_plane(pos[0], pos[2], [*normal, d])
            flag[i] = True
        return bbox3d, flag

    def get_samples(self, ground, non_ground, calib_, plane_, grid=None, ues_plane_filter=True):
        if grid is None:
            samples, xyz_ = self.sample_xyz(plane_)
            radius = 3
            ues_plane_filter = True
        else:
            samples, xyz_, scene_type = self.sample_from_grid(grid)
            radius = {'a': 4, 'b': 3, 'c': 2, 'd': 1}[scene_type]

        samples, bbox3d_ = self.xyz_to_bbox3d(samples, xyz_, calib_, random_flip=self.random_flip)

        flag1 = np.ones((bbox3d_.shape[0]), dtype=bool)
        # 判断样本是否在地面上，第一次筛除
        if ues_plane_filter:
            bbox3d_, flag1 = self.sample_put_on_plane(bbox3d_, ground, radius=radius, min_num=10, max_degree=15)

        if flag1.sum() == 0:
            return []

        # api 要求 bbox3d 为 lidar 坐标系
        bbox3d_in_lidar = rect2lidar(bbox3d_[flag1], calib_)

        # 判断样本间是否有重叠，第二次筛除
        iou = boxes_bev_iou_cpu(bbox3d_in_lidar, bbox3d_in_lidar)
        iou[range(bbox3d_in_lidar.shape[0]), range(bbox3d_in_lidar.shape[0])] = 0
        rows, cols = np.triu_indices(n=iou.shape[0], k=1)
        iou[rows, cols] = 0
        flag2 = iou.max(axis=1) == 0
        if flag2.sum() == 0:
            return []

        # 判断样本是否与障碍物重叠，第三次筛除
        points_in_lidar = calib_.rect_to_lidar(non_ground)
        flag3 = ~ check_points_in_boxes3d(points_in_lidar, bbox3d_in_lidar[flag2])
        if flag3.sum() == 0:
            return []

        # 合并筛除结果
        valid = np.arange(bbox3d_.shape[0])[flag1][flag2][flag3]
        res = [Sample(samples[i], bbox3d_[i], calib_, self) for i in valid]
        return res

    @staticmethod
    def add_samples_to_scene(samples, image, depth, max_num=10, use_edge_blur=False):
        image_, depth_ = image.copy(), depth.copy()
        mask = np.zeros(image.shape[:2], dtype=bool)
        samples = random.sample(samples, np.min([max_num, len(samples)]))
        samples = sorted(samples, key=lambda x: x.bbox3d_[2], reverse=True)  # z 降序
        flag = np.zeros(len(samples), dtype=bool)
        for i, sample in enumerate(samples):
            image_, depth_, mask, flag[i] = sample.cover(image_, depth_, mask)

        if use_edge_blur:
            blur = cv2.GaussianBlur(image_, (3, 3), 0)
            kernel = np.ones((3, 3), np.uint8)
            mask_ = cv2.erode(mask.astype(np.uint8), kernel, iterations=1)
            blur_place = mask_.astype(bool) != mask
            image_[blur_place] = blur[blur_place]
        return image_, depth_, [sample for i, sample in enumerate(samples) if flag[i]]


class Sample:
    def __init__(self, sample, bbox3d, calib, database: SampleDatabase):
        self.sample = sample
        self.bbox3d_ = bbox3d
        self.database = database

        self.label = sample['label']
        self.calib = sample['calib']
        self.alpha_ = sample['label'].alpha
        self.calib_ = calib
        self.plane = sample['plane']
        self.bbox2d = sample['bbox2d']
        self.name = sample['name']

        self.flipped = sample.get('flipped', False)
        self.image = self.get_image(flip=self.flipped)
        self.depth = self.get_depth(flip=self.flipped)

        self.occlusion_ = 0  # 需要在最终图像中求
        self.trucation_ = 0
        self.image_, self.depth_, self.bbox2d_ = self.transform()

    def __repr__(self):
        return f"Sample(name={self.name})"

    def get_image(self, flip=False):
        try:
            image = self.database.sample_image_database[self.name]
        except KeyError:
            image_file = self.database.image_path / (self.name + ".png")
            assert image_file.exists()
            image = cv2.imread(str(image_file))
        if flip:
            image = cv2.flip(image, 1)
        return image

    def get_depth(self, flip=False):
        try:
            depth = self.database.sample_depth_database[self.name]
        except KeyError:
            depth_file = self.database.depth_path / (self.name + ".png")
            assert depth_file.exists()
            depth = cv2.imread(str(depth_file), cv2.IMREAD_UNCHANGED) / 256.0
        if flip:
            depth = cv2.flip(depth, 1)
        return depth

    def get_points(self):
        assert self.depth.shape[:2] == self.image.shape[:2]
        image, depth, calib, label = self.image, self.depth, self.calib, self.label
        calib_, bbox3d_, bbox2d = self.calib_, self.bbox3d_, self.bbox2d
        xyz, ry = label.pos, label.ry
        xyz_, ry_ = bbox3d_[:3], bbox3d_[6]

        cord, rgb = to3d(image, depth, calib, bbox2d)

        # 删除 d = 0 的点
        valid = cord[:, 2] >= 1e-3
        cord, rgb = cord[valid], rgb[valid]

        cord = cord - xyz
        dr = ry_ - ry
        R = np.array([[np.cos(dr), 0, np.sin(dr)],
                      [0, 1, 0],
                      [-np.sin(dr), 0, np.cos(dr)]])
        cord = cord @ R.T + xyz_

        return cord, rgb

    @staticmethod
    def get_3d_center_in_2d(xyz, calib):  # kitti 的 pos 是底部中心, xyz 需要为实际中心
        xyz = xyz.reshape(1, -1)[:, :3]
        uv, _ = calib.rect_to_img(xyz)
        uv = np.round(uv).astype(int).reshape(2)
        return uv

    def transform(self):
        assert self.depth.shape[:2] == self.image.shape[:2]
        image, depth, calib, label = self.image, self.depth, self.calib, self.label
        calib_, bbox3d_, bbox2d, alpha_ = self.calib_, self.bbox3d_, self.bbox2d, self.alpha_

        center = self.get_3d_center_in_2d(label.pos + [0, -label.h / 2, 0], calib)
        center_ = self.get_3d_center_in_2d(bbox3d_[:3] + [0, -bbox3d_[4] / 2, 0], calib_)

        dry = bbox3d_[6] - label.ry  # ry_ - ry
        h, w = depth.shape

        offset = np.arange(w, dtype=int) - center[0] + bbox2d[0]

        width = abs(np.sin(alpha_) * label.w) + abs(np.cos(alpha_) * label.l)
        offset = - np.tan(dry) * offset * width / w

        depth_ = depth - label.pos[2] + offset.reshape(1, -1) + bbox3d_[2]
        depth_[depth < 1e-2] = 0

        rate = bbox3d_[2] / label.pos[2]  # z_ / z
        h_, w_ = round(h / rate), round(w / rate)

        depth_ = cv2.resize(depth_, (w_, h_), interpolation=cv2.INTER_NEAREST)
        image_ = cv2.resize(image, (w_, h_), interpolation=cv2.INTER_NEAREST)

        bbox2d_ = np.tile((bbox2d[:2] - center) / rate + center_, 2)
        bbox2d_ = np.round(bbox2d_).astype(int)
        # 这里 bbox2d_ 是可能在背景图像外的
        bbox2d_[2:] += [w_, h_]  # 避免 bbox2d_ 与 image_ 的大小不一致

        return image_, depth_, bbox2d_.tolist()

    @staticmethod
    def truncate(image_, depth_, bbox2d_, image_shape):
        h, w = image_shape[:2]
        u_min, v_min, u_max, v_max = bbox2d_
        area = (v_max - v_min) * (u_max - u_min)
        if u_min < 0:
            image_ = image_[:, -u_min:]
            depth_ = depth_[:, -u_min:]
            bbox2d_[0] = 0
        if v_min < 0:
            image_ = image_[-v_min:, :]
            depth_ = depth_[-v_min:, :]
            bbox2d_[1] = 0
        if u_max > w:
            image_ = image_[:, :w - u_max]
            depth_ = depth_[:, :w - u_max]
            bbox2d_[2] = w
        if v_max > h:
            image_ = image_[:h - v_max, :]
            depth_ = depth_[:h - v_max, :]
            bbox2d_[3] = h
        area_ = (bbox2d_[3] - bbox2d_[1]) * (bbox2d_[2] - bbox2d_[0])
        truncate_rate = (area - area_) / area
        return image_, depth_, bbox2d_, truncate_rate

    def cover(self, image, depth, mask, area_threshold=0.5):
        assert image.shape[:2] == depth.shape
        blank_rgb, blank_d, mask = image.copy(), depth.copy(), mask.copy()
        image_, depth_, bbox2d_ = self.image_, self.depth_, self.bbox2d_
        # 截取图像外的样本
        image_, depth_, bbox2d_, self.trucation_ = self.truncate(image_, depth_, bbox2d_, image.shape)

        u_min, v_min, u_max, v_max = bbox2d_
        if u_min >= u_max or v_min >= v_max:  # 可能发生
            return blank_rgb, blank_d, mask, False

        d_in_bbox2d = blank_d[v_min: v_max, u_min: u_max]
        valid = (depth_ > 1e-2) & (depth_ < d_in_bbox2d)
        area = (v_max - v_min) * (u_max - u_min) - np.sum(depth_ <= 1e-2)
        valid_rate = np.sum(valid) / area
        if valid_rate <= area_threshold:
            return blank_rgb, blank_d, mask, False

        blank_rgb[v_min: v_max, u_min: u_max][valid] = image_[valid]
        blank_d[v_min: v_max, u_min: u_max][valid] = depth_[valid]
        mask[v_min: v_max, u_min: u_max][valid] = True

        return blank_rgb, blank_d, mask, True

    def to_label(self):
        label = self.label
        cls = label.cls_type
        trucation = self.trucation_
        score = 0
        occlusion = 0
        x_, y_, z_, l_, h_, w_, ry_ = self.bbox3d_
        alpha = self.get_alpha(self.bbox3d_[:3], ry_, self.calib_)
        u_min, v_min, u_max, v_max = self.bbox2d_
        line = f"{cls} {trucation} {occlusion} {alpha} {u_min} {v_min} {u_max} {v_max} {h_} {w_} {l_} {x_} {y_} {z_} {ry_} {score}"
        res = Object3d(line)
        res.is_fake = True
        return res

    @staticmethod
    def get_alpha(xyz, ry, calib):
        uv, _ = calib.rect_to_img(xyz.reshape(1, -1))
        alpha = calib.ry2alpha(ry, uv[:, 0])[0]
        return alpha


from pathlib import Path
import time
import datetime

if __name__ == '__main__':
    test_dir = Path("/mnt/e/DataSet/kitti/kitti_inst_database/test")
    np.random.seed(0)

    database = SampleDatabase("/mnt/e/DataSet/kitti/kitti_inst_database/")
    dataset = Dataset("train", r"/mnt/e/DataSet/kitti")
    mean_samples = 0
    n = 200
    dt = 0
    for idx in range(n):
        calib_ = dataset.get_calib(idx)
        image, depth = dataset.get_image_with_depth(idx, use_penet=True)
        ground, non_ground = dataset.get_lidar_with_ground(idx, fov=True)
        plane_ = dataset.get_plane(idx)
        grid = dataset.get_grid(idx)
        _, _, labels = dataset.get_bbox(idx, chosen_cls=["Car", 'Van', 'Truck', 'DontCare'])

        time1 = time.time()
        samples = database.get_samples(ground, non_ground, calib_, plane_, grid=grid)
        image_, depth_, samples = database.add_samples_to_scene(samples, image, depth, use_edge_blur=True)
        labels = merge_labels(labels, samples, calib_, image.shape)
        time2 = time.time()

        for label in labels:
            cv2.putText(image_, str(round(label.area, 2)), (int(label.box2d[0]), int(label.box2d[1])),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        mean_samples += len(samples)
        cv2.imwrite(str(test_dir / ('%06d.png' % idx)), image_)
        dt += time2 - time1
    print("time: ", dt / n)
    print(mean_samples / n)
