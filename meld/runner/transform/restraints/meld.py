#
# All rights reserved
#

"""
This module implements transformers that add meld restraints
"""

import logging

logger = logging.getLogger(__name__)

from meld.runner.transform.restraints.util import _delete_from_always_active
from meld import interfaces
from meld.system import restraints
from meld.system import options
from meld.system import param_sampling
from meld.runner import transform
from meldplugin import MeldForce  # type: ignore

from simtk import openmm as mm  # type: ignore
from simtk.openmm import app  # type: ignore

import numpy as np  # type: ignore
from typing import List, Tuple, Optional


class _RestraintTracker:
    """
    A data structure to keep track of restraints, groups, and collections.

    Each restraint, group, or collection is added to the appropriate list
    in the order that it was added to the MeldForce. This allows us to
    later enumerate the list in the same order when updating the MeldForce.

    The always active collection will never be updated, so it is added as
    None. Similarly, every restraint in the always active collection
    will have its own group that will never be udpated, so these singleton
    groups are added as None.
    """

    distance_restraints: List[restraints.DistanceRestraint]
    hyperbolic_distance_restraints: List[restraints.HyperbolicDistanceRestraint]
    torsion_restraints: List[restraints.TorsionRestraint]
    dist_prof_restraints: List[restraints.DistProfileRestraint]
    torsion_profile_restraints: List[restraints.TorsProfileRestraint]
    gmm_restraints: List[restraints.GMMDistanceRestraint]
    groups: List[Optional[restraints.RestraintGroup]]
    collections: List[Optional[restraints.SelectivelyActiveCollection]]

    def __init__(self):
        self.distance_restraints = []
        self.hyperbolic_distance_restraints = []
        self.torsion_restraints = []
        self.dist_prof_restraints = []
        self.torsion_profile_restraints = []
        self.gmm_restraints = []
        self.groups = []
        self.collections = []


class MeldRestraintTransformer(transform.TransformerBase):
    """
    Transformer to handle MELD restraints
    """

    force: MeldForce

    def __init__(
        self,
        param_manager: param_sampling.ParameterManager,
        options: options.RunOptions,
        always_active_restraints: List[restraints.Restraint],
        selectively_active_restraints: List[restraints.SelectivelyActiveCollection],
    ) -> None:
        # We use the param_manager to update parameters that can be sampled over.
        self.param_manager = param_manager

        # Track indices of restraints, groups, and collections so that we can
        # update them.
        self.tracker = _RestraintTracker()

        self.always_on = [
            r
            for r in always_active_restraints
            if isinstance(r, restraints.SelectableRestraint)
        ]
        _delete_from_always_active(self.always_on, always_active_restraints)

        # Gather all of the selectively active restraints.
        self.selective_on = [r for r in selectively_active_restraints]
        for r in self.selective_on:
            selectively_active_restraints.remove(r)

        if self.always_on or self.selective_on:
            self.active = True
        else:
            self.active = False

    def add_interactions(
        self, state: interfaces.IState, system: mm.System, topology: app.Topology
    ) -> mm.System:
        if self.active:
            meld_force = MeldForce()

            # Add all of the always-on restraints
            if self.always_on:
                group_list = []
                for rest in self.always_on:
                    rest_index = _add_meld_restraint(
                        self.tracker, rest, meld_force, 0, 0
                    )
                    # Each restraint goes in its own group.
                    group_index = meld_force.addGroup([rest_index], 1)
                    group_list.append(group_index)

                    # Add the group to the tracker, but as None, so
                    # we won't update it.
                    self.tracker.groups.append(None)

                # All of the always-on restraints go in a single collection
                meld_force.addCollection(group_list, len(group_list))

                # Add this collection to the tracker, but as
                # None, so we won't update it
                self.tracker.collections.append(None)

            # Add the selectively active restraints
            for coll in self.selective_on:
                group_indices = []
                for group in coll.groups:
                    restraint_indices = []
                    for rest in group.restraints:
                        rest_index = _add_meld_restraint(
                            self.tracker, rest, meld_force, 0, 0
                        )
                        restraint_indices.append(rest_index)

                    # Create the group in the meldplugin
                    group_num_active = self._handle_num_active(group.num_active, state)
                    group_index = meld_force.addGroup(
                        restraint_indices, group_num_active
                    )
                    group_indices.append(group_index)

                    # Add the group to the tracker so we can update it
                    self.tracker.groups.append(group)

                # Create the collection in the meldplugin
                coll_num_active = self._handle_num_active(group.num_active, state)
                meld_force.addCollection(group_indices, coll_num_active)

                # Add the collection to the tracker so we can update it
                self.tracker.collections.append(coll)

            system.addForce(meld_force)
            self.force = meld_force
        return system

    def update(
        self,
        state: interfaces.IState,
        simulation: app.Simulation,
        alpha: float,
        timestep: int,
    ) -> None:
        if self.active:
            self._update_restraints(alpha, timestep)
            self._update_groups_collections(state)
            self.force.updateParametersInContext(simulation.context)

    def _update_groups_collections(
        self,
        state: interfaces.IState,
    ) -> None:
        for i, coll in enumerate(self.tracker.collections):
            if coll is None:
                continue
            num_active = self._handle_num_active(coll.num_active, state)
            self.force.modifyCollectionNumActive(i, num_active)

        for i, group in enumerate(self.tracker.groups):
            if group is None:
                continue
            num_active = self._handle_num_active(group.num_active, state)
            self.force.modifyGroupNumActive(i, num_active)

    def _update_restraints(
        self,
        alpha: float,
        timestep: int,
    ) -> None:
        for i, dist_rest in enumerate(self.tracker.distance_restraints):
            scale = dist_rest.scaler(alpha) * dist_rest.ramp(timestep)
            self.force.modifyDistanceRestraint(
                i,
                dist_rest.atom_index_1,
                dist_rest.atom_index_2,
                dist_rest.r1(alpha),
                dist_rest.r2(alpha),
                dist_rest.r3(alpha),
                dist_rest.r4(alpha),
                dist_rest.k * scale,
            )

        for i, hyper_rest in enumerate(self.tracker.hyperbolic_distance_restraints):
            scale = hyper_rest.scaler(alpha) * hyper_rest.ramp(timestep)
            self.force.modifyHyperbolicDistanceRestraint(
                i,
                hyper_rest.atom_index_1,
                hyper_rest.atom_index_2,
                hyper_rest.r1,
                hyper_rest.r2,
                hyper_rest.r3,
                hyper_rest.r4,
                hyper_rest.k * scale,
                hyper_rest.asymptote * scale,
            )

        for i, tors_rest in enumerate(self.tracker.torsion_restraints):
            scale = tors_rest.scaler(alpha) * tors_rest.ramp(timestep)
            self.force.modifyTorsionRestraint(
                i,
                tors_rest.atom_index_1,
                tors_rest.atom_index_2,
                tors_rest.atom_index_3,
                tors_rest.atom_index_4,
                tors_rest.phi,
                tors_rest.delta_phi,
                tors_rest.k * scale,
            )

        for i, dist_prof_rest in enumerate(self.tracker.dist_prof_restraints):
            scale = dist_prof_rest.scaler(alpha) * dist_prof_rest.ramp(timestep)
            self.force.modifyDistProfileRestraint(
                i,
                dist_prof_rest.atom_index_1,
                dist_prof_rest.atom_index_2,
                dist_prof_rest.r_min,
                dist_prof_rest.r_max,
                dist_prof_rest.n_bins,
                dist_prof_rest.spline_params[:, 0],
                dist_prof_rest.spline_params[:, 1],
                dist_prof_rest.spline_params[:, 2],
                dist_prof_rest.spline_params[:, 3],
                dist_prof_rest.scale_factor * scale,
            )

        for i, tors_prof_rest in enumerate(self.tracker.torsion_profile_restraints):
            scale = tors_prof_rest.scaler(alpha) * tors_prof_rest.ramp(timestep)
            self.force.modifyTorsProfileRestraint(
                i,
                tors_prof_rest.atom_index_1,
                tors_prof_rest.atom_index_2,
                tors_prof_rest.atom_index_3,
                tors_prof_rest.atom_index_4,
                tors_prof_rest.atom_index_5,
                tors_prof_rest.atom_index_6,
                tors_prof_rest.atom_index_7,
                tors_prof_rest.atom_index_8,
                tors_prof_rest.n_bins,
                tors_prof_rest.spline_params[:, 0],
                tors_prof_rest.spline_params[:, 1],
                tors_prof_rest.spline_params[:, 2],
                tors_prof_rest.spline_params[:, 3],
                tors_prof_rest.spline_params[:, 4],
                tors_prof_rest.spline_params[:, 5],
                tors_prof_rest.spline_params[:, 6],
                tors_prof_rest.spline_params[:, 7],
                tors_prof_rest.spline_params[:, 8],
                tors_prof_rest.spline_params[:, 9],
                tors_prof_rest.spline_params[:, 10],
                tors_prof_rest.spline_params[:, 11],
                tors_prof_rest.spline_params[:, 12],
                tors_prof_rest.spline_params[:, 13],
                tors_prof_rest.spline_params[:, 14],
                tors_prof_rest.spline_params[:, 15],
                tors_prof_rest.scale_factor * scale,
            )

        for i, gmm_rest in enumerate(self.tracker.gmm_restraints):
            scale = gmm_rest.scaler(alpha) * gmm_rest.ramp(timestep)
            nd = gmm_rest.n_distances
            nc = gmm_rest.n_components
            w = gmm_rest.weights
            m = list(gmm_rest.means.flatten())
            d, o = _setup_precisions(gmm_rest.precisions, nd, nc)
            self.force.modifyGMMRestraint(i, nd, nc, scale, gmm_rest.atoms, w, m, d, o)

    def _handle_num_active(self, value, state):
        if isinstance(value, param_sampling.Parameter):
            return int(self.param_manager.extract_value(value, state.parameters))
        else:
            return value


def _add_meld_restraint(
    tracker: _RestraintTracker, rest, meld_force: MeldForce, alpha: float, timestep: int
) -> int:
    scale = rest.scaler(alpha) * rest.ramp(timestep)

    if isinstance(rest, restraints.DistanceRestraint):
        rest_index = meld_force.addDistanceRestraint(
            rest.atom_index_1,
            rest.atom_index_2,
            rest.r1(alpha),
            rest.r2(alpha),
            rest.r3(alpha),
            rest.r4(alpha),
            rest.k * scale,
        )
        tracker.distance_restraints.append(rest)

    elif isinstance(rest, restraints.HyperbolicDistanceRestraint):
        rest_index = meld_force.addHyperbolicDistanceRestraint(
            rest.atom_index_1,
            rest.atom_index_2,
            rest.r1,
            rest.r2,
            rest.r3,
            rest.r4,
            rest.k * scale,
            rest.asymptote * scale,
        )
        tracker.hyperbolic_distance_restraints.append(rest)

    elif isinstance(rest, restraints.TorsionRestraint):
        rest_index = meld_force.addTorsionRestraint(
            rest.atom_index_1,
            rest.atom_index_2,
            rest.atom_index_3,
            rest.atom_index_4,
            rest.phi,
            rest.delta_phi,
            rest.k * scale,
        )
        tracker.torsion_restraints.append(rest)

    elif isinstance(rest, restraints.DistProfileRestraint):
        rest_index = meld_force.addDistProfileRestraint(
            rest.atom_index_1,
            rest.atom_index_2,
            rest.r_min,
            rest.r_max,
            rest.n_bins,
            rest.spline_params[:, 0],
            rest.spline_params[:, 1],
            rest.spline_params[:, 2],
            rest.spline_params[:, 3],
            rest.scale_factor * scale,
        )
        tracker.dist_prof_restraints.append(rest)

    elif isinstance(rest, restraints.TorsProfileRestraint):
        rest_index = meld_force.addTorsProfileRestraint(
            rest.atom_index_1,
            rest.atom_index_2,
            rest.atom_index_3,
            rest.atom_index_4,
            rest.atom_index_5,
            rest.atom_index_6,
            rest.atom_index_7,
            rest.atom_index_8,
            rest.n_bins,
            rest.spline_params[:, 0],
            rest.spline_params[:, 1],
            rest.spline_params[:, 2],
            rest.spline_params[:, 3],
            rest.spline_params[:, 4],
            rest.spline_params[:, 5],
            rest.spline_params[:, 6],
            rest.spline_params[:, 7],
            rest.spline_params[:, 8],
            rest.spline_params[:, 9],
            rest.spline_params[:, 10],
            rest.spline_params[:, 11],
            rest.spline_params[:, 12],
            rest.spline_params[:, 13],
            rest.spline_params[:, 14],
            rest.spline_params[:, 15],
            rest.scale_factor * scale,
        )
        tracker.torsion_profile_restraints.append(rest)

    elif isinstance(rest, restraints.GMMDistanceRestraint):
        nd = rest.n_distances
        nc = rest.n_components
        w = rest.weights
        m = list(rest.means.flatten())

        d, o = _setup_precisions(rest.precisions, nd, nc)
        rest_index = meld_force.addGMMRestraint(nd, nc, scale, rest.atoms, w, m, d, o)
        tracker.gmm_restraints.append(rest)

    else:
        raise RuntimeError(f"Do not know how to handle restraint {rest}")

    return rest_index


def _setup_precisions(
    precisions: np.ndarray, n_distances: int, n_conditions: int
) -> Tuple[List[float], List[float]]:
    # The normalization of our GMMs will blow up
    # due to division by zero if the precisions
    # are zero, so we clamp this to a very
    # small value.
    diags = []
    for i in range(n_conditions):
        for j in range(n_distances):
            diags.append(precisions[i, j, j])

    off_diags = []
    for i in range(n_conditions):
        for j in range(n_distances):
            for k in range(j + 1, n_distances):
                off_diags.append(precisions[i, j, k])

    return diags, off_diags
